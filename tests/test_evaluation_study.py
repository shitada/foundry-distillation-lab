from contextlib import redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
import importlib
import importlib.util
from io import StringIO
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import Mock, patch
import uuid

from foundry_distillation_lab.evaluation.study import MODELS, PAIRS, _allocate_directory, study
from foundry_distillation_lab.evaluation.workflow import _load_run, compare_runs
from foundry_distillation_lab.io import read_json, sha256
from foundry_distillation_lab.retail import RetailSession


ROOT = Path(__file__).resolve().parents[1]
STUDY = importlib.import_module("foundry_distillation_lab.evaluation.study")


class StudyTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / "runs" / f"study-test-{uuid.uuid4().hex}"
        self.directory.mkdir(parents=True)
        session = RetailSession()
        self.bundle = {
            "schema": "retail-evaluation-input-v1", "models": list(MODELS),
            "tools": session.tools, "records": [],
            "cases": [{
                "case_id": "order", "category": "照会", "user_input": "注文 ORD-001 を確認してください。",
                "system_prompt": session.system_prompt,
                "expected": {
                    "initial_tools": ["get_order_details"],
                    "required_calls": [{"name": "get_order_details", "arguments": {"order_id": "ORD-001"}}],
                    "allowed_mutations": [],
                    "final_state": {"terminal": "answer_only", "submissions": []}},
                "reference": {"role": "assistant", "content": "参照回答 ORACLE_ONLY"},
            }],
        }
        self.config = {
            "base_url": "https://example.invalid/openai/v1/",
            "targets": {model: f"secret-{model}-target" for model in MODELS},
            "max_completion_tokens": 128, "timeout_seconds": 30,
            "max_model_calls": 3, "max_tool_calls": 2,
        }
        self.grader = {"base_url": "https://judge.invalid/openai/v1/", "deployment": "judge-target",
                       "max_completion_tokens": 128, "timeout_seconds": 30}
        self.input = self.directory / "input.json"
        self.config_path = self.directory / "config.json"
        self.grading_path = self.directory / "grading.json"
        self.output = self.directory / "e2e"
        self.save()
        self.requests, self.judgments = [], []
        spec = importlib.util.spec_from_file_location("study_cli", ROOT / "scripts" / "evaluate.py")
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def save(self):
        for path, value in ((self.input, self.bundle), (self.config_path, self.config),
                            (self.grading_path, self.grader)):
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def invoke(self, request):
        self.requests.append(deepcopy(request))
        message = ({"content": None, "tool_calls": [{
            "id": "lookup", "type": "function",
            "function": {"name": "get_order_details", "arguments": '{"order_id":"ORD-001"}'}}]}
            if len(request["messages"]) == 2 else
            {"content": "不正確な回答" if request["model_label"] == "base" else "注文を確認しました。"})
        return {"message": message, "response_model": "private-evaluated-version",
                "response_id": f"answer-{len(self.requests)}",
                "usage": {"input_tokens": 10, "output_tokens": 4, "cached_input_tokens": 0}}

    def judge(self, request):
        self.judgments.append(deepcopy(request))
        data = json.loads(request["messages"][1]["content"])
        bad = data["candidate"]["answer"] == "不正確な回答"
        return {"message": {"content": json.dumps({
            "decision": "failure" if bad else "success", "reason": "不正確です。" if bad else "根拠と整合します。"})},
            "finish_reason": "stop", "response_model": "judge-version",
            "response_id": f"judge-{len(self.judgments)}",
            "usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 0}}

    def run_study(self, **kwargs):
        return study(self.input, self.config_path, self.grading_path, self.output,
                     invoke=kwargs.pop("invoke", self.invoke), judge=kwargs.pop("judge", self.judge),
                     **kwargs)

    def test_three_model_e2e_reports_and_pairwise_invariants(self):
        summary = self.run_study()
        self.assertTrue(summary["complete"])
        self.assertEqual(summary, read_json(self.output / "summary.json"))
        self.assertEqual(len(self.requests), 6)
        self.assertEqual(len(self.judgments), 3)
        self.assertEqual(summary["comparisons"]["base-vs-fine-tuned"]["decision"], "improvement")
        self.assertEqual(summary["comparisons"]["teacher-vs-fine-tuned"]["decision"], "no_change")
        self.assertEqual(summary["comparisons"]["teacher-vs-base"]["decision"], "regression")
        self.assertEqual(summary["grading"]["judge_calls"], 3)
        self.assertEqual(summary["grading"]["usage_total"]["input_tokens"], 300)
        for model in MODELS:
            item = summary["per_model"][model]
            self.assertEqual(item["denominator"], 1)
            self.assertEqual(item["usage_total"]["input_tokens"], 20)
            self.assertEqual(item["grading"]["usage_total"]["input_tokens"], 100)
            self.assertEqual(item["latency_seconds"]["n"], 1)
            self.assertGreaterEqual(item["latency_seconds"]["p95"], 0)
            self.assertEqual(item["confirmed_business_successes"], 0)
            self.assertEqual(item["automatic_decision_counts"]["failure" if model == "base" else "success"], 1)
            _, evidence, execution = _load_run(self.output / f"{model}-graded")
            self.assertEqual(execution["config"], self.config)
            self.assertIn("response_id", evidence["records"][0])
            self.assertEqual(execution["source_input_sha256"], sha256(self.input))
        for before, after, name in PAIRS:
            compared = compare_runs(self.output / f"{before}-graded", self.output / f"{after}-graded",
                                    self.directory / f"verify-{name}")
            self.assertEqual(compared, read_json(self.output / name / "comparison.json"))
            self.assertTrue((self.output / name / "comparison.md").exists())
        table = self.output / "comparison.csv"
        self.assertTrue(table.read_bytes().startswith(b"\xef\xbb\xbf"))
        with table.open(encoding="utf-8-sig", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["teacher_verdict"], "success")
        self.assertEqual(row["base_verdict"], "failure")
        self.assertEqual(row["fine_tuned_reason"], "根拠と整合します。")
        self.assertEqual(json.loads(row["base_response"])["answer"], "不正確な回答")
        self.assertEqual(row["teacher_input_tokens"], "20")
        self.assertEqual(json.loads(row["expected"]), self.bundle["cases"][0]["expected"])
        self.assertEqual(len(list(self.output.rglob("*.csv"))), 4)
        self.assertFalse(list(self.output.rglob("reviews.csv")))
        self.assertFalse((self.output / "evidence.json").exists())

    def test_pristine_sessions_and_blinded_no_oracle_payloads(self):
        second = deepcopy(self.bundle["cases"][0])
        second["case_id"] = "order-again"
        self.bundle["cases"].append(second)
        self.save()
        sessions = []
        original_call = RetailSession.call

        def checked_call(session, name, arguments):
            self.assertFalse(hasattr(session._store, "_study_touched"))
            session._store._study_touched = True
            sessions.append(session)
            return original_call(session, name, arguments)

        with patch.object(RetailSession, "call", checked_call):
            self.run_study()
        self.assertEqual(len(sessions), 6)
        self.assertEqual(len({id(session._store) for session in sessions}), 6)
        starts = [request for request in self.requests if len(request["messages"]) == 2]
        self.assertEqual(len(starts), 6)
        self.assertTrue(all(request["messages"] == starts[0]["messages"] for request in starts))
        for request in self.requests:
            self.assertEqual(set(request), {"attempt_id", "model_label", "target", "messages", "tools"})
            self.assertNotIn("ORACLE_ONLY", json.dumps(request))
            self.assertNotIn("expected", request)
        for request in self.judgments:
            data = json.dumps(request, ensure_ascii=False)
            self.assertIn("ORACLE_ONLY", data)
            for secret in ("secret-", "private-evaluated-version", "model_label", "provenance"):
                self.assertNotIn(secret, data)
            self.assertEqual(request["target"], "judge-target")

    def test_repeated_invocation_allocates_new_folder_without_touching_prior_evidence(self):
        first = self.run_study()
        original = {str(path.relative_to(self.output)): path.read_bytes()
                    for path in self.output.rglob("*") if path.is_file()}
        second = self.run_study()
        third = self.run_study()
        self.assertEqual(Path(first["output_dir"]).name, "e2e")
        self.assertEqual(Path(second["output_dir"]).name, "e2e-002")
        self.assertEqual(Path(third["output_dir"]).name, "e2e-003")
        self.assertEqual(original, {str(path.relative_to(self.output)): path.read_bytes()
                                   for path in self.output.rglob("*") if path.is_file()})
        self.assertEqual(len(self.requests), 18)

    def test_exclusive_allocator_handles_file_collision_and_exhaustion(self):
        self.output.write_text("keep", encoding="utf-8")
        allocated = _allocate_directory(self.output)
        self.assertEqual(allocated.name, "e2e-002")
        self.assertEqual(self.output.read_text(encoding="utf-8"), "keep")
        with patch.object(Path, "mkdir", side_effect=FileExistsError) as mkdir:
            with self.assertRaisesRegex(FileExistsError, "allocation_exhausted"):
                _allocate_directory(self.output)
        self.assertEqual(mkdir.call_count, 10000)

    def test_concurrent_allocations_never_share_a_directory(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            directories = list(pool.map(_allocate_directory, [self.output] * 8))
        self.assertEqual(len(set(directories)), 8)
        self.assertEqual({path.name for path in directories},
                         {"e2e", *(f"e2e-{index:03d}" for index in range(2, 9))})

    def test_prevalidate_all_targets_input_and_grader_before_output_or_send(self):
        baseline = deepcopy((self.bundle, self.config, self.grader))
        changes = [
            lambda: self.config["targets"].pop("fine_tuned"),
            lambda: self.config["targets"].update(teacher=""),
            lambda: self.bundle.update(models=["teacher", "base"]),
            lambda: self.grader.update(max_completion_tokens=0),
            lambda: self.grader.update(timeout_seconds=601),
            lambda: self.config.update(max_model_calls=0),
            lambda: self.config.update(extra=True),
            lambda: self.bundle["cases"][0].update(system_prompt="wrong"),
            lambda: self.bundle["cases"].append({**deepcopy(self.bundle["cases"][0]),
                                               "case_id": "late-invalid", "user_input": ""}),
        ]
        for change in changes:
            self.bundle, self.config, self.grader = deepcopy(baseline)
            change()
            self.save()
            with self.assertRaises(ValueError):
                self.run_study()
            self.assertFalse(self.output.exists())
            self.assertEqual(self.requests, [])
            self.assertEqual(self.judgments, [])

    def test_frozen_input_and_configs_are_shared_despite_external_midflight_edits(self):
        expected_digest = sha256(self.input)

        def mutate(request):
            self.input.write_text("{}", encoding="utf-8")
            self.config_path.write_text("{}", encoding="utf-8")
            self.grading_path.write_text("{}", encoding="utf-8")
            return self.invoke(request)

        summary = self.run_study(invoke=mutate)
        self.assertTrue(summary["complete"])
        for model in MODELS:
            execution = read_json(self.output / model / "execution.json")
            self.assertEqual(execution["source_input_sha256"], expected_digest)
            self.assertEqual(execution["config"], self.config)

    def test_failed_source_preserves_denominator_continues_other_models_without_retry(self):
        second = deepcopy(self.bundle["cases"][0])
        second["case_id"] = "unattempted-for-base"
        self.bundle["cases"].append(second)
        self.save()
        attempts = []

        def fail_base(request):
            attempts.append(request["model_label"])
            if request["model_label"] == "base":
                raise TimeoutError("uncertain request")
            return self.invoke(request)

        summary = self.run_study(invoke=fail_base)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual(attempts.count("base"), 1)
        self.assertEqual(attempts.count("teacher"), 4)
        self.assertEqual(attempts.count("fine_tuned"), 4)
        self.assertEqual(summary["per_model"]["base"]["denominator"], 2)
        self.assertEqual(summary["per_model"]["base"]["observed_records"], 1)
        self.assertEqual(summary["per_model"]["base"]["automatic_decision_counts"]["unknown"], 2)
        self.assertIsNone(summary["per_model"]["base"]["usage_total"]["input_tokens"])
        self.assertEqual(summary["per_model"]["base"]["grading"]["missing_records"], 1)
        self.assertEqual(summary["comparisons"]["base-vs-fine-tuned"]["decision"], "incomparable")
        self.assertEqual(summary["comparisons"]["teacher-vs-fine-tuned"]["decision"], "no_change")
        self.assertTrue(summary["errors"])
        self.assertEqual(read_json(self.output / "base" / "record-000001.json")["error_type"], "TimeoutError")
        self.assertTrue(list((self.output / "base").rglob("*")))

    def test_grader_failure_is_unknown_and_no_retry_but_valid_uncertainty_can_complete(self):
        failed = Mock(side_effect=TimeoutError("judge unknown"))
        summary = self.run_study(judge=failed)
        self.assertFalse(summary["complete"])
        self.assertEqual(failed.call_count, 3)
        self.assertEqual(summary["grading"]["judge_failures"], 3)
        self.assertEqual(summary["comparisons"]["teacher-vs-base"]["decision"], "incomparable")

        def uncertain(request):
            result = self.judge(request)
            result["message"]["content"] = json.dumps({"decision": "needs_review", "reason": "根拠不足。"})
            return result

        summary = self.run_study(judge=uncertain)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["comparisons"]["teacher-vs-base"]["decision"], "incomparable")

    def test_configured_model_call_cap_remains_enforced(self):
        self.config["max_model_calls"] = 1
        self.save()
        summary = self.run_study()
        self.assertFalse(summary["complete"])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len(self.judgments), 0)
        for model in MODELS:
            record = read_json(self.output / model / "record-000001.json")
            self.assertEqual(record["status"], "limit_exceeded")
            self.assertEqual(record["model_calls"], 1)
            self.assertEqual(record["tool_calls"], 1)

    def test_unexpected_error_saves_failed_summary_and_propagates_with_actual_path(self):
        self.output.mkdir()
        with patch.object(STUDY, "run_evaluation", side_effect=RuntimeError("unexpected")):
            with self.assertRaisesRegex(RuntimeError, "unexpected") as caught:
                self.run_study()
        actual = self.directory / "e2e-002"
        summary = read_json(actual / "summary.json")
        self.assertEqual(summary["status"], "failed")
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["errors"][0]["error_type"], "RuntimeError")
        self.assertEqual(summary["steps"][-1]["state"], "started")
        self.assertIn(str(actual), caught.exception.__notes__[0])
        self.assertEqual(self.requests, [])

    def test_mismatched_grader_identity_cannot_be_presented_as_comparable(self):
        def changing_judge(request):
            response = self.judge(request)
            response["response_model"] = f"version-{len(self.judgments)}"
            return response

        with self.assertRaisesRegex(ValueError, "grader_response_model_mismatch"):
            self.run_study(judge=changing_judge)
        summary = read_json(self.output / "summary.json")
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["status"], "failed")
        self.assertFalse((self.output / "comparison.csv").exists())

    def test_cli_output_exit_codes_and_standard_unexpected_error(self):
        argv = ["study", "--input", str(self.input), "--config", str(self.config_path),
                "--grading-config", str(self.grading_path), "--output-dir", str(self.output)]
        out = StringIO()
        with patch.object(self.cli, "study", side_effect=lambda *args: study(
                *args, invoke=self.invoke, judge=self.judge)), redirect_stdout(out):
            self.assertEqual(self.cli.main(argv), 0)
        self.assertIn(str(self.output), out.getvalue())
        self.assertIn("comparison.csv", out.getvalue())
        out = StringIO()
        with patch.object(self.cli, "study", side_effect=lambda *args: study(
                *args, invoke=Mock(side_effect=TimeoutError), judge=self.judge)), redirect_stdout(out):
            self.assertEqual(self.cli.main(argv), 2)
        self.assertIn("e2e-002", out.getvalue())
        self.assertIn("incomplete", out.getvalue())
        err = StringIO()
        with patch.object(STUDY, "run_evaluation", side_effect=RuntimeError("broken")), redirect_stderr(err):
            self.assertEqual(self.cli.main(argv), 2)
        self.assertIn("RuntimeError: broken", err.getvalue())
        self.assertIn("e2e-003", err.getvalue())
        self.assertFalse(read_json(self.directory / "e2e-003" / "summary.json")["complete"])


if __name__ == "__main__":
    unittest.main()
