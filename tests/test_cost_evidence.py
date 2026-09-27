"""Offline cost inventories must never replay work or manufacture known costs."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import unittest
import uuid

from foundry_distillation_lab.datasets.prepare import normalize, prepare
from foundry_distillation_lab.evaluation.cases import prepare_cases
from foundry_distillation_lab.evaluation.scoring import prepare_next_actions
from foundry_distillation_lab.evaluation.study import study
from foundry_distillation_lab.evaluation.workflow import run_evaluation
from foundry_distillation_lab.io import read_json, read_jsonl, sha256
from foundry_distillation_lab.reporting.evidence import discover_evidence
from foundry_distillation_lab.safety import Journal
from foundry_distillation_lab.training.collection import import_traces
from foundry_distillation_lab.training.jobs import prepare as prepare_training


ROOT = Path(__file__).resolve().parents[1]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / ".test-runs" / f"cost-evidence-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.data = self.root / "data"
        prepare(ROOT / "data" / "samples" / "traces.jsonl", self.data)
        self.input = self.root / "cases.json"
        prepare_cases(self.data, self.input)
        bundle = read_json(self.input)
        # The independent missing-order case produces one eligible judge call.
        bundle["cases"] = [bundle["cases"][-1]]
        save(self.input, bundle)
        self.config = {
            "base_url": "https://example.invalid/openai/v1/",
            "targets": {"teacher": "teacher", "base": "base", "fine_tuned": "ft:base:1"},
            "max_completion_tokens": 100, "timeout_seconds": 30,
            "max_model_calls": 2, "max_tool_calls": 2,
        }
        self.config_path = self.root / "config.json"
        save(self.config_path, self.config)
        self.judge_path = self.root / "judge.json"
        save(self.judge_path, {"base_url": "https://judge.invalid/openai/v1/", "deployment": "judge",
                               "max_completion_tokens": 100, "timeout_seconds": 30})
        self.counter = 0

    def tearDown(self):
        shutil.rmtree(self.root)

    def respond(self, request):
        self.counter += 1
        judge = request["target"] == "judge"
        return {
            "message": {"content": json.dumps({"decision": "success", "reason": "確認しています。"})
                        if judge else "注文番号を教えてください。"},
            "finish_reason": "stop", "response_id": f"response-{self.counter}",
            "response_model": request["target"],
            "usage": {"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 0},
        }

    def study(self, name="study"):
        return Path(study(self.input, self.config_path, self.judge_path, self.root / name,
                          invoke=self.respond, judge=self.respond)["output_dir"])

    def test_standard_mock_study_and_derivative_copies_are_deduplicated(self):
        directory = self.study()
        before = {p: sha256(p) for p in self.root.rglob("*") if p.is_file()}
        first = discover_evidence(self.root)
        self.assertEqual(first["study_dir"], str(directory))
        self.assertEqual(set(first["graded_dirs"]), {"teacher", "base", "fine_tuned"})
        self.assertEqual(len(first["calls"]), 6)
        self.assertTrue(first["evaluation_complete"])
        self.assertFalse(first["training_complete"])
        self.assertTrue(first["generation"]["known"])
        self.assertIn("scripted", first["generation"]["zero_cost_reason"])
        self.assertEqual({p: sha256(p) for p in before}, before)
        shutil.copytree(directory, self.root / "copy")
        # Copied studies have the exact same started timestamp; either has same evidence.
        result = discover_evidence(self.root)
        self.assertEqual(len(result["calls"]), 6)
        self.assertEqual(len([c for c in result["calls"] if c["kind"] == "grading"]), 3)
        self.assertEqual({c["variant"] for c in result["calls"]}, {"teacher", "base", "fine_tuned"})

    def test_newest_started_not_name_mtime_or_success_and_retry_costs(self):
        older = self.study("study-999")
        newer = self.study("study-002")
        # Remove the result and all final evidence after one request began.
        for path in (newer / "teacher" / "attempts").glob("*.result.json"):
            path.unlink()
        for name in ("execution.json", "evidence.json", "scores.json"):
            (newer / "teacher" / name).unlink()
        for model in ("teacher", "base", "fine_tuned"):
            shutil.rmtree(newer / f"{model}-graded")
        for model in ("base", "fine_tuned"):
            shutil.rmtree(newer / model)
        save(older / "summary.json", {"complete": True})
        save(newer / "summary.json", {"status": "failed", "complete": False})
        result = discover_evidence(self.root)
        self.assertEqual(result["study_dir"], str(newer))
        self.assertEqual(result["graded_dirs"], {})
        self.assertEqual(len(result["calls"]), 7)
        unknown = [c for c in result["calls"] if c["outcome"] == "outcome_unknown"]
        self.assertEqual(len(unknown), 1)
        self.assertTrue(all(v is None for v in unknown[0]["usage"].values()))
        self.assertTrue(any("selected graded" in d["reason"] for d in result["diagnostics"]))
        self.assertFalse(result["evaluation_complete"])

    def test_actual_timestamp_order_and_selected_corruption_no_fallback(self):
        first = self.study("study-010")
        last = self.study("study-002")
        for directory, timestamp in ((first, "2026-01-01T00:00:00+00:00"),
                                     (last, "2025-12-31T20:00:00-05:00")):
            start = read_json(directory / "study-start.json")
            start["started_at"] = timestamp
            save(directory / "study-start.json", start)
        (last / "input.json").write_text("{}", encoding="utf-8")
        result = discover_evidence(self.root)
        self.assertEqual(result["study_dir"], str(last))
        self.assertFalse(result["calls"])
        self.assertTrue(any("snapshot hash mismatch" in d["reason"] for d in result["diagnostics"]))

    def test_next_action_retries_linked_by_dataset_hash_not_unrelated(self):
        next_path = self.root / "next.json"
        bundle = prepare_next_actions(read_jsonl(self.data / "next-actions.jsonl"),
                                      sha256(self.data / "next-actions.jsonl"))
        save(next_path, bundle)
        phase_six_config = deepcopy(self.config)
        del phase_six_config["targets"]["teacher"]
        phase_six_path = self.root / "phase-six-config.json"
        save(phase_six_path, phase_six_config)
        for name in ("next", "next-retry"):
            run_evaluation(next_path, phase_six_path, "next-action", "base",
                           self.root / name, invoke=self.respond)
        bad = deepcopy(bundle)
        bad["source"]["sha256"] = "f" * 64
        save(next_path, bad)
        run_evaluation(next_path, phase_six_path, "next-action", "base",
                       self.root / "unrelated", invoke=self.respond)
        self.study()
        result = discover_evidence(self.root)
        self.assertEqual(len(result["calls"]), 6 + 2 * len(bundle["cases"]))
        self.assertTrue(any("unrelated task/dataset" in x["reason"] for x in result["exclusions"]))

    def test_same_data_different_case_retry_is_initial_cost_only(self):
        selected = self.study()
        retry = read_json(self.input)
        retry["cases"][0]["case_id"] = "earlier-case-version"
        retry_path = self.root / "retry-input.json"
        save(retry_path, retry)
        run_evaluation(retry_path, self.config_path, "e2e", "base",
                       self.root / "earlier-evaluation", invoke=self.respond)
        result = discover_evidence(self.root)
        self.assertEqual(len(result["calls"]), 7)
        self.assertEqual(result["study_dir"], str(selected))
        self.assertEqual(result["graded_dirs"]["base"], str(selected / "base-graded"))

    def training(self, name, job_id, tokens):
        config = self.root / "train-config.json"
        save(config, {"project_endpoint": "https://example.services.ai.azure.com/api/projects/p",
                      "model": "base", "training_type": "GlobalStandard",
                      "hyperparameters": {"n_epochs": 3}})
        directory = self.root / name
        plan = prepare_training(self.data / "train.jsonl", self.data / "validation.jsonl", config, directory)
        response = {"id": job_id, "status": "running", "trained_tokens": tokens, "created_at": 100}
        save(directory / "job-receipt.json", {
            "schema_version": 1, "kind": "training-job-receipt", "target": plan["target"],
            "response": response})
        return directory, plan

    def test_training_retries_latest_job_journal_and_null_tokens(self):
        self.study()
        first, plan = self.training("train", "job-one", 120)
        self.training("train-retry", "job-two", 80)
        shutil.copytree(first, self.root / "train-copy")
        payload = {"operation": "training-status", "target": plan["target"],
                   "input_sha256": sha256(first / "upload-plan.json"),
                   "request": {"method": "GET", "kind": "job", "id": "job-one"}}
        journal = Journal(first)
        journal.start("status-latest", payload)
        response = {"id": "job-one", "status": "failed", "trained_tokens": None,
                    "created_at": 100, "finished_at": 200}
        journal.finish("status-latest", "response_received", {"response": response})
        save(first / "observations" / "latest.json", {"kind": "job", "id": "job-one", "response": response})
        result = discover_evidence(self.root)
        self.assertEqual(len(result["training"]), 2)
        jobs = {j["job_id"]: j for j in result["training"]}
        self.assertIsNone(jobs["job-one"]["trained_tokens"])
        self.assertEqual(jobs["job-one"]["status"], "failed")
        self.assertEqual(jobs["job-two"]["trained_tokens"], 80)  # Not multiplied by epochs.

    def test_unrelated_training_hashes_excluded(self):
        self.study()
        directory, plan = self.training("other-training", "other", 500)
        plan["files"]["train"]["source_sha256"] = "0" * 64
        save(directory / "upload-plan.json", plan)
        result = discover_evidence(self.root)
        self.assertEqual(result["training"], [])
        self.assertTrue(any("unrelated training dataset" in d["reason"] for d in result["exclusions"]))

    def test_invalid_matching_evaluation_does_not_leave_known_partial_total(self):
        older = self.study("older")
        self.study("newer")
        save(older / "base" / "execution-start.json", {"schema": "invalid"})
        result = discover_evidence(self.root)
        self.assertTrue(result["calls"])
        self.assertFalse(result["evaluation_complete"])
        self.assertTrue(any("invalid evaluation evidence" in d["reason"] for d in result["diagnostics"]))

    def test_missing_grading_journal_does_not_leave_known_partial_total(self):
        directory = self.study()
        for path in (directory / "base-graded" / "attempts").glob("*.json"):
            path.unlink()
        result = discover_evidence(self.root)
        self.assertEqual(len(result["calls"]), 5)
        self.assertFalse(result["evaluation_complete"])
        self.assertTrue(any("missing recorded judge calls" in d["reason"] for d in result["diagnostics"]))

    def test_unusable_matching_training_invalidates_other_known_jobs_total(self):
        self.study()
        self.training("valid-training", "valid", 100)
        bad, _ = self.training("bad-training", "bad", 200)
        (bad / "job-receipt.json").write_text("{broken", encoding="utf-8")
        result = discover_evidence(self.root)
        self.assertEqual(len(result["training"]), 1)
        self.assertEqual(result["training"][0]["trained_tokens"], 100)
        self.assertFalse(result["training_complete"])
        self.assertTrue(any("invalid training evidence" in d["reason"] for d in result["diagnostics"]))

    def test_complete_known_training_jobs_total_flag(self):
        self.study()
        self.training("training", "valid", 100)
        result = discover_evidence(self.root)
        self.assertTrue(result["training_complete"])

    def test_uncertain_training_submission_is_retained_without_receipt(self):
        self.study()
        directory, plan = self.training("train", "lost", None)
        (directory / "job-receipt.json").unlink()
        Journal(directory).start("training-submit-lost", {"target": plan["target"]})
        result = discover_evidence(self.root)
        self.assertEqual(len(result["training"]), 1)
        self.assertIsNone(result["training"][0]["trained_tokens"])
        self.assertIsNone(result["training"][0]["job_id"])
        self.assertEqual(result["training"][0]["status"], "outcome_unknown")

    def test_received_error_preserves_raw_usage_not_record_summary(self):
        directory = self.study()
        path = next((directory / "teacher" / "attempts").glob("*.result.json"))
        result = read_json(path)
        result["result"]["response"]["response_error"] = "interrupted after usage"
        result["result"]["response"]["usage"]["output_tokens"] = None
        save(path, result)
        report = discover_evidence(self.root)
        call = next(c for c in report["calls"] if c["kind"] == "evaluation" and c["variant"] == "teacher")
        self.assertEqual(call["usage"]["input_tokens"], 10)
        self.assertIsNone(call["usage"]["output_tokens"])

    def imported_data(self, usage=True, source_format="conversation"):
        rows = read_jsonl(ROOT / "data" / "samples" / "traces.jsonl")
        for index, row in enumerate(rows):
            row["source_kind"] = "exported_trace_unreviewed"
            row["source"] = "https://example.services.ai.azure.com/api/projects/p"
            row["model"] = "teacher"
            row["response_id"] = f"generation-{index}"
            if usage:
                row["usage"] = {"prompt_tokens": 100, "completion_tokens": 20,
                                "prompt_tokens_details": {"cached_tokens": 5}}
        source = self.root / "export.jsonl"
        exported = rows
        if source_format == "responses-capture":
            exported = []
            for row in rows:
                normalized, _ = normalize(row)
                messages = []
                for message in normalized["messages"][1:-1]:
                    if message["role"] == "tool":
                        messages.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                                         "output": message["content"]})
                    else:
                        if message.get("content"):
                            messages.append({"role": message["role"], "content": message["content"]})
                        for call in message.get("tool_calls", []):
                            messages.append({"type": "function_call", "call_id": call["id"],
                                             **call["function"]})
                exported.append({
                    "kind": "foundry-responses-capture", "conversation_id": row["conversation_id"],
                    "category": row["category"], "call_index": 1,
                    "source": {"target": row["source"] + "|" + row["model"],
                               "collection_plan_sha256": "a" * 64},
                    "request": {"model": row["model"], "instructions": normalized["messages"][0]["content"],
                                "input": messages, "tools": [
                                    {"type": "function", **tool["function"]} for tool in normalized["tools"]]},
                    "response": {"id": row["response_id"], "status": "completed", "usage": row.get("usage"),
                                 "output": [{"type": "message", "role": "assistant", "content": [
                                     {"type": "output_text", "text": normalized["messages"][-1]["content"]}]}]},
                })
        source.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in exported), encoding="utf-8")
        import_traces(source, self.root / "collection", source_format)
        shutil.rmtree(self.data)
        prepare(self.root / "collection" / "traces.jsonl", self.data)
        self.input.unlink()
        prepare_cases(self.data, self.input)
        bundle = read_json(self.input)
        bundle["cases"] = [bundle["cases"][-1]]
        save(self.input, bundle)
        return rows

    def test_real_generation_missing_usage_is_unknown_not_zero(self):
        self.imported_data(usage=False)
        self.study()
        result = discover_evidence(self.root)
        self.assertFalse(result["generation"]["known"])
        self.assertIsNone(result["generation"]["zero_cost_reason"])
        self.assertTrue(any(c["kind"] == "generation" and c["usage"]["input_tokens"] is None
                            for c in result["calls"]))

    def test_collection_usage_and_copies_are_bound_and_deduplicated(self):
        rows = self.imported_data()
        self.study()
        shutil.copytree(self.root / "collection", self.root / "collection-copy")
        result = discover_evidence(self.root)
        self.assertTrue(result["generation"]["known"])
        generated = [c for c in result["calls"] if c["kind"] == "generation"]
        self.assertEqual(len(generated), len(rows))
        self.assertTrue(all(c["variant"] == "fine_tuned" for c in generated))
        self.assertEqual(generated[0]["usage"],
                         {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 5})

    def test_capture_import_source_target_supplies_validated_generation_endpoint(self):
        rows = self.imported_data(source_format="responses-capture")
        self.study()
        result = discover_evidence(self.root)
        self.assertTrue(result["generation"]["known"])
        generated = [call for call in result["calls"] if call["kind"] == "generation"]
        self.assertEqual(len(generated), len(rows))
        for call in generated:
            self.assertEqual(call["endpoint"], rows[0]["source"])
            self.assertEqual(call["target"], "teacher")
            self.assertEqual(call["source_metadata"], {
                "target": rows[0]["source"] + "|teacher", "collection_plan_sha256": "a" * 64})

    def test_raw_invocation_recovers_manifest_usage_without_double_count(self):
        rows = self.imported_data()
        manifest_path = self.root / "collection" / "collection-manifest.json"
        manifest = read_json(manifest_path)
        manifest["observations"][0]["known_usage"] = None
        save(manifest_path, manifest)
        self.study()
        directory = self.root / "invoke"
        row = rows[0]
        save(directory / "invocation-plan.json", {
            "schema_version": 1, "kind": "hosted-invocation", "model": row["model"],
            "project_endpoint": row["source"], "agent_endpoint": row["source"] + "/agents/a/versions/1",
            "input": {"conversation_id": row["conversation_id"]}})
        journal = Journal(directory)
        journal.start("collect-capture", {"conversation_id": row["conversation_id"]})
        journal.finish("collect-capture", "response_received", {"response": {
            "response": {"id": row["response_id"], "model": row["model"], "usage": row["usage"]}}})
        result = discover_evidence(self.root)
        self.assertTrue(result["generation"]["known"])
        calls = [c for c in result["calls"] if c["kind"] == "generation"]
        self.assertEqual(len(calls), len(rows))
        self.assertTrue(all(c["usage"]["input_tokens"] == 100 for c in calls))

    def test_missing_real_acquisition_and_corrupt_source_never_become_zero(self):
        self.imported_data()
        self.study()
        shutil.rmtree(self.root / "collection")
        result = discover_evidence(self.root)
        self.assertFalse(result["generation"]["known"])
        self.assertIsNone(result["generation"]["zero_cost_reason"])
        (self.data / "train.jsonl").write_text("{}\n", encoding="utf-8")
        result = discover_evidence(self.root)
        self.assertTrue(any("snapshot hash mismatch" in d["reason"] for d in result["diagnostics"]))

    def test_cost_report_subtrees_and_symlinks_not_traversed(self):
        selected = self.study()
        shutil.copytree(selected, self.root / "cost-report" / "study")
        copied = self.root / "cost-report" / "study" / "study-start.json"
        start = read_json(copied)
        start["started_at"] = "2099-01-01T00:00:00Z"
        save(copied, start)
        result = discover_evidence(self.root)
        self.assertEqual(result["study_dir"], str(selected))
        self.assertEqual(len(result["calls"]), 6)
        link = self.root / "linked"
        try:
            link.symlink_to(selected, target_is_directory=True)
        except OSError:
            return  # Windows may disallow link creation without developer mode.
        try:
            result = discover_evidence(self.root)
            self.assertTrue(any("symlink" in d["reason"] for d in result["diagnostics"]))
            self.assertEqual(len(result["calls"]), 6)
        finally:
            link.unlink()


if __name__ == "__main__":
    unittest.main()
