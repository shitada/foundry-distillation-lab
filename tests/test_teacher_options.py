from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
import uuid

from foundry_distillation_lab.evaluation.grading import RESPONSE_FORMAT, grade_run
from foundry_distillation_lab.evaluation.runner import OpenAITransport
from foundry_distillation_lab.evaluation.schema import ContractError
from foundry_distillation_lab.evaluation.workflow import (
    _load_run, _validate, combine_runs, compare_runs, effective_model_settings, run_evaluation,
)
from foundry_distillation_lab.io import read_json


ROOT = Path(__file__).resolve().parents[1]
TEACHER_SETTINGS = {
    "reasoning_effort": "high", "max_completion_tokens": 16384, "timeout_seconds": 300,
}
DEFAULT_SETTINGS = {"max_completion_tokens": 1024, "timeout_seconds": 60}


class TeacherOptionsTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / "runs" / f"teacher-options-test-{uuid.uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.bundle = read_json(ROOT / "data" / "samples" / "evaluation-next-action.json")
        self.bundle.update(models=["teacher", "base", "fine_tuned"], records=[])
        self.bundle["cases"] = [{
            "case_id": "text", "messages": [{"role": "user", "content": "Say hello."}],
            "expected": {"kind": "text", "calls": []},
            "reference": {"role": "assistant", "content": "Hello."},
        }]
        self.config = {
            "base_url": "https://example.invalid/openai/v1/",
            "targets": {"teacher": "gpt-5.5", "base": "student-base", "fine_tuned": "student-fine"},
            **DEFAULT_SETTINGS, "max_model_calls": 12, "max_tool_calls": 24,
            "model_settings": {"teacher": deepcopy(TEACHER_SETTINGS)},
        }
        self.input_path = self.directory / "input.json"
        self.config_path = self.directory / "config.json"
        self.input_path.write_text(json.dumps(self.bundle), encoding="utf-8")
        azure, identity, openai = (ModuleType(name) for name in ("azure", "azure.identity", "openai"))
        azure.identity = identity
        identity.DefaultAzureCredential = Mock()
        identity.get_bearer_token_provider = Mock(return_value="test-token-provider")
        self.client = Mock()
        self.create = self.client.chat.completions.create
        self.create.return_value.model_dump.return_value = {
            "choices": [{"finish_reason": "stop",
                         "message": {"role": "assistant", "content": "Hello."}}],
            "model": "test-response-version", "id": "test-response-id",
            "usage": {"prompt_tokens": 10, "completion_tokens": 4,
                      "prompt_tokens_details": {"cached_tokens": 0}},
        }
        self.openai = openai.OpenAI = Mock(return_value=self.client)
        sdk = patch.dict(sys.modules, {"azure": azure, "azure.identity": identity, "openai": openai})
        sdk.start()
        self.addCleanup(sdk.stop)

    def run_model(self, model="teacher", name=None):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        directory = self.directory / (name or model)
        _, complete = run_evaluation(
            self.input_path, self.config_path, "next-action", model, directory)
        self.assertTrue(complete)
        return directory

    def test_sdk_teacher_overrides_students_defaults_and_durable_snapshots(self):
        original = deepcopy(self.config)
        for model in ("teacher", "base", "fine_tuned"):
            with self.subTest(model=model):
                expected = TEACHER_SETTINGS if model == "teacher" else DEFAULT_SETTINGS

                def respond(**kwargs):
                    start = read_json(self.directory / model / "execution-start.json")
                    self.assertEqual(start["generation_settings"], expected)
                    return self.create.return_value

                self.create.side_effect = respond
                directory = self.run_model(model)
                kwargs = self.create.call_args.kwargs
                self.assertEqual(kwargs["model"], self.config["targets"][model])
                self.assertEqual(kwargs["max_completion_tokens"], expected["max_completion_tokens"])
                if model == "teacher":
                    self.assertEqual(kwargs["reasoning_effort"], "high")
                else:
                    self.assertNotIn("reasoning_effort", kwargs)
                self.assertFalse(kwargs["store"])
                self.assertFalse(kwargs["parallel_tool_calls"])
                self.assertEqual(kwargs["tools"], self.bundle["tools"])
                self.assertEqual(self.openai.call_args.kwargs["timeout"], expected["timeout_seconds"])
                self.assertEqual(self.openai.call_args.kwargs["max_retries"], 0)
                execution = read_json(directory / "execution.json")
                self.assertEqual(execution["generation_settings"], expected)
                self.assertEqual(execution["config"], original)
                _load_run(directory)
        self.assertEqual(self.create.call_count, 3)
        self.assertEqual(self.config, original)

    def test_partial_overrides_and_helper_do_not_mutate_config(self):
        for override in ({"reasoning_effort": "high"}, {"max_completion_tokens": 16384},
                         {"timeout_seconds": 300}):
            with self.subTest(override=override):
                self.config["model_settings"]["teacher"] = override
                original = deepcopy(self.config)
                settings = effective_model_settings(self.config, "teacher")
                self.assertEqual(settings, {**DEFAULT_SETTINGS, **override})
                _validate(self.bundle, self.config, "next-action", "teacher")
                settings["max_completion_tokens"] = 1
                self.assertEqual(self.config, original)
                self.assertEqual(effective_model_settings(self.config, "base"), DEFAULT_SETTINGS)
        self.openai.assert_not_called()

    def test_invalid_overrides_on_unselected_teacher_fail_before_requests(self):
        invalid = [None, [], "", {"unknown": TEACHER_SETTINGS},
                   {"teacher": {}}, {"teacher": []}, {"teacher": None},
                   {"teacher": {"temperature": 1}}]
        for field, values in (
                ("max_completion_tokens", (True, False, 0, -1, 1.5, "100", None)),
                ("timeout_seconds", (True, False, 0, -1, 601, "60", None,
                                     float("nan"), float("inf"), float("-inf"))),
                ("reasoning_effort", (None, True, 1, [], {}, "", "HIGH", "unsupported"))):
            invalid.extend({"teacher": {field: value}} for value in values)
        for index, overrides in enumerate(invalid):
            with self.subTest(overrides=overrides):
                self.config["model_settings"] = overrides
                with self.assertRaises(ValueError):
                    self.run_model("base", f"invalid-{index}")
                self.assertFalse((self.directory / f"invalid-{index}").exists())
        self.openai.assert_not_called()
        self.create.assert_not_called()

    def test_global_validation_is_not_masked_by_valid_teacher_overrides(self):
        original = deepcopy(self.config)
        for field, value in (
                ("max_completion_tokens", False), ("timeout_seconds", float("inf")),
                ("max_model_calls", 0), ("max_tool_calls", True),
                ("base_url", "http://example.invalid/openai/v1/"),
                ("targets", {"teacher": "gpt-5.5", "base": ""})):
            with self.subTest(field=field):
                config = {**original, field: value}
                with self.assertRaises(ValueError):
                    _validate(self.bundle, config, "next-action", "teacher")
        for field in original.keys() - {"model_settings"}:
            config = deepcopy(original)
            del config[field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                _validate(self.bundle, config, "next-action", "teacher")
        self.openai.assert_not_called()

    def test_grader_does_not_inherit_teacher_options(self):
        source = self.run_model()
        grader = {
            "base_url": "https://grader.invalid/openai/v1/", "deployment": "grader",
            **DEFAULT_SETTINGS,
        }
        path = self.directory / "grader.json"
        path.write_text(json.dumps(grader), encoding="utf-8")
        self.create.return_value.model_dump.return_value["choices"][0]["message"]["content"] = json.dumps({
            "decision": "success", "reason": "The response matches the requested greeting.",
        })
        _, complete = grade_run(source, path, self.directory / "graded")
        self.assertTrue(complete)
        kwargs = self.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "grader")
        self.assertEqual(kwargs["max_completion_tokens"], 1024)
        self.assertNotIn("reasoning_effort", kwargs)
        self.assertNotIn("tools", kwargs)
        self.assertEqual(kwargs["response_format"], RESPONSE_FORMAT)
        self.assertFalse(kwargs["store"])
        self.assertEqual(self.openai.call_args.kwargs["timeout"], 60)
        self.assertEqual(self.openai.call_args.kwargs["max_retries"], 0)
        self.assertEqual(self.create.call_count, 2)
        _, _, execution = _load_run(self.directory / "graded")
        self.assertEqual(execution["generation_settings"], TEACHER_SETTINGS)

    def test_same_full_config_allows_combining_and_comparing_models(self):
        teacher, base = self.run_model(), self.run_model("base")
        report = combine_runs([teacher, base], self.directory / "combined")
        self.assertEqual(set(report["per_model"]), {"teacher", "base"})
        compare_runs(teacher, base, self.directory / "comparison")
        self.config["model_settings"]["teacher"]["reasoning_effort"] = "low"
        changed = self.run_model("base", "changed-map")
        for operation in (
                lambda: combine_runs([teacher, changed], self.directory / "bad-combined"),
                lambda: compare_runs(teacher, changed, self.directory / "bad-comparison")):
            with self.assertRaisesRegex(ContractError, "inference_mismatch"):
                operation()
        self.assertEqual(self.create.call_count, 3)

    def test_generation_snapshot_mismatch_is_rejected(self):
        directory = self.run_model()
        path = directory / "execution.json"
        execution = read_json(path)
        for settings in (None, {}, DEFAULT_SETTINGS, {**TEACHER_SETTINGS, "reasoning_effort": "low"},
                         {**TEACHER_SETTINGS, "timeout_seconds": True},
                         {**TEACHER_SETTINGS, "extra": 1}):
            with self.subTest(settings=settings):
                path.write_text(json.dumps({**execution, "generation_settings": settings}), encoding="utf-8")
                with self.assertRaisesRegex(ContractError, "generation_settings_mismatch"):
                    _load_run(directory)
        self.assertEqual(self.create.call_count, 1)

    def test_six_field_config_and_legacy_runs_remain_supported(self):
        self.config.pop("model_settings")
        directory = self.run_model()
        self.assertNotIn("reasoning_effort", self.create.call_args.kwargs)
        self.assertEqual(self.create.call_args.kwargs["max_completion_tokens"], 1024)
        self.assertEqual(self.openai.call_args.kwargs["timeout"], 60)
        path = directory / "execution.json"
        execution = read_json(path)
        self.assertEqual(execution.pop("generation_settings"), DEFAULT_SETTINGS)
        path.write_text(json.dumps(execution), encoding="utf-8")
        self.assertEqual(_load_run(directory)[2], execution)
        base = self.run_model("base")
        combine_runs([directory, base], self.directory / "legacy-combined")
        self.assertEqual(self.create.call_count, 2)

    def test_constructor_reasoning_values_and_omission(self):
        payload = {"target": "gpt-5.5", "messages": [], "tools": []}
        for effort in ("none", "minimal", "low", "medium", "high", "xhigh"):
            transport = OpenAITransport(base_url=self.config["base_url"], reasoning_effort=effort)
            transport(payload)
            self.assertEqual(self.create.call_args.kwargs["reasoning_effort"], effort)
        for effort in ("", "HIGH", "unsupported", False, 1, [], {}):
            with self.subTest(effort=effort), self.assertRaises(ValueError):
                OpenAITransport(base_url=self.config["base_url"], reasoning_effort=effort)
        transport = OpenAITransport(base_url=self.config["base_url"])
        transport(payload)
        self.assertNotIn("reasoning_effort", self.create.call_args.kwargs)
