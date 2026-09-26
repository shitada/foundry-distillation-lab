"""Exercise separate model runs through human review and the existing cost adapter."""

import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from foundry_distillation_lab.evaluation.runner import OpenAITransport
from foundry_distillation_lab.io import read_json, write_json
from foundry_distillation_lab.reporting.bridge import prepare_from_files
from foundry_distillation_lab.retail import RetailSession


ROOT = Path(__file__).resolve().parents[1]


class WorkflowBridgeTests(unittest.TestCase):
    def test_separate_reviewed_runs_combine_without_losing_cost_evidence(self):
        spec = importlib.util.spec_from_file_location("evaluation_bridge_cli", ROOT / "scripts" / "evaluate.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        retail = RetailSession()
        labels = ("teacher", "base", "fine_tuned")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = root / "cases.json"
            write_json(cases, {
                "schema": "retail-evaluation-input-v1",
                "evidence_kind": "synthetic_fixture",
                "models": list(labels), "records": [], "tools": retail.tools,
                "source": {"kind": "synthetic_test_fixture"},
                "cases": [{
                    "case_id": "fixture-1", "category": "clarification",
                    "user_input": "注文について問い合わせたいです。",
                    "system_prompt": retail.system_prompt,
                    "expected": {"initial_tools": [], "required_calls": [],
                                 "allowed_mutations": [],
                                 "final_state": {"terminal": "answer_only", "submissions": []}},
                }],
            })
            config = root / "config.json"
            write_json(config, {
                "base_url": "https://example.invalid/openai/v1/",
                "targets": {label: f"fixture-{label}" for label in labels},
                "max_completion_tokens": 1024, "timeout_seconds": 60,
                "max_model_calls": 12, "max_tool_calls": 24,
            })
            response = {"message": {"role": "assistant", "content": "注文番号を教えてください。"},
                        "usage": {"input_tokens": 20, "cached_input_tokens": 0, "output_tokens": 8},
                        "model": "synthetic-response-model", "response_id": "synthetic-response-id"}
            reviewed = []
            with patch.object(OpenAITransport, "__call__", return_value=response) as send:
                for label in labels:
                    run_dir = root / label
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(cli.main([
                            "run", "--mode", "e2e", "--input", str(cases),
                            "--config", str(config), "--model", label, "--run-dir", str(run_dir),
                        ]), 0)
                    review_path = run_dir / "reviews.csv"
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(cli.main(["review-sheet", "--run-dir", str(run_dir)]), 0)
                    with review_path.open(encoding="utf-8-sig", newline="") as stream:
                        reader = csv.DictReader(stream)
                        fields, rows = reader.fieldnames, list(reader)
                    self.assertEqual(len(rows), 1)
                    rows[0].update(decision="success", reviewer="fixture-reviewer",
                                   notes="Synthetic fixture: clarification without a business mutation.")
                    with review_path.open("w", encoding="utf-8", newline="") as stream:
                        writer = csv.DictWriter(stream, fieldnames=fields)
                        writer.writeheader()
                        writer.writerows(rows)
                    destination = root / f"{label}-reviewed"
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(cli.main([
                            "review", "--run-dir", str(run_dir), "--output-dir", str(destination),
                        ]), 0)
                    reviewed.append(destination)
                self.assertEqual(send.call_count, 3)
                combined = root / "combined"
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main([
                        "combine", "--run-dirs", *map(str, reviewed), "--output-dir", str(combined),
                    ]), 0)
                self.assertEqual(send.call_count, 3, "Combining or reviewing must never send model requests")
            scores = read_json(combined / "scores.json")
            self.assertEqual(scores["scheduled_slots"], 3)
            self.assertEqual(scores["observed_records"], 3)
            self.assertEqual(set(scores["per_model"]), set(labels))
            for row in scores["rows"]:
                self.assertEqual(row["status"], "confirmed_success")
                self.assertEqual(row["review"]["evidence_sha256"], row["evidence_sha256"])
            prepared = prepare_from_files(
                combined / "scores.json", ROOT / "configs" / "examples" / "cost-evaluation.json",
                root / "cost-input.json")
            self.assertTrue(prepared["evaluation_bridge"]["same_conditions"])
            for variant in prepared["variants"].values():
                self.assertEqual(variant["usage_per_request"],
                                 {"input_tokens": 20, "cached_input_tokens": 0, "output_tokens": 8})
                self.assertTrue(variant["quality"]["review_complete"])
                self.assertEqual(variant["quality"]["successful_requests"], 1)

    def test_automatic_grade_compare_and_costs_without_review_csv_input(self):
        spec = importlib.util.spec_from_file_location("automatic_grade_cli", ROOT / "scripts" / "evaluate.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        retail = RetailSession()
        labels = ("teacher", "base", "fine_tuned")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "cases.json", {
                "schema": "retail-evaluation-input-v1", "models": list(labels),
                "evidence_kind": "synthetic_test", "source": {"kind": "synthetic_test"},
                "records": [], "tools": retail.tools, "cases": [{
                    "case_id": "clarification", "category": "synthetic_test",
                    "user_input": "注文について問い合わせたいです。",
                    "system_prompt": retail.system_prompt,
                    "expected": {"initial_tools": [], "required_calls": [], "allowed_mutations": [],
                                 "final_state": {"terminal": "answer_only", "submissions": []}},
                }],
            })
            write_json(root / "evaluation.json", {
                "base_url": "https://example.invalid/openai/v1/",
                "targets": {name: f"fixture-{name}" for name in labels},
                "max_completion_tokens": 1024, "timeout_seconds": 60,
                "max_model_calls": 12, "max_tool_calls": 24,
            })
            write_json(root / "grading.json", {
                "base_url": "https://example.invalid/openai/v1/", "deployment": "grader",
                "max_completion_tokens": 1024, "timeout_seconds": 60,
            })
            calls = {"model": 0, "grader": 0}

            def respond(payload):
                if payload["target"] == "grader":
                    calls["grader"] += 1
                    task = json.loads(payload["messages"][1]["content"])
                    decision = "failure" if task["candidate"]["answer"] == "わかりません。" else "success"
                    return {"message": {"role": "assistant", "content": json.dumps({
                        "decision": decision, "reason": "確認質問の有無を確認した架空の採点結果です。",
                    }, ensure_ascii=False)}, "finish_reason": "stop",
                        "response_model": "synthetic-grader", "response_id": "synthetic-grade-id",
                        "usage": {"input_tokens": 900, "output_tokens": 100, "cached_input_tokens": 0}}
                calls["model"] += 1
                return {"message": {"role": "assistant", "content": (
                    "わかりません。" if payload["target"] == "fixture-base" else "注文番号を教えてください。")},
                    "usage": {"input_tokens": 20, "output_tokens": 8, "cached_input_tokens": 0}}

            with patch.object(OpenAITransport, "__call__", side_effect=respond), \
                    contextlib.redirect_stdout(io.StringIO()):
                for label in labels:
                    self.assertEqual(cli.main([
                        "run", "--mode", "e2e", "--input", str(root / "cases.json"),
                        "--config", str(root / "evaluation.json"), "--model", label,
                        "--run-dir", str(root / label),
                    ]), 0)
                    self.assertEqual(cli.main([
                        "grade", "--run-dir", str(root / label), "--config", str(root / "grading.json"),
                        "--output-dir", str(root / f"{label}-graded"),
                    ]), 0)
                self.assertEqual(list(root.rglob("reviews.csv")), [])
                self.assertEqual(cli.main([
                    "compare", "--before", str(root / "base-graded"),
                    "--after", str(root / "fine_tuned-graded"), "--output-dir", str(root / "comparison"),
                ]), 0)
                self.assertEqual(cli.main([
                    "combine", "--run-dirs", *[str(root / f"{label}-graded") for label in labels],
                    "--output-dir", str(root / "combined"),
                ]), 0)
            self.assertEqual(calls, {"model": 3, "grader": 3})
            self.assertEqual(read_json(root / "comparison" / "comparison.json")["decision"], "improvement")
            self.assertTrue((root / "comparison" / "comparison.csv").is_file())
            scores = read_json(root / "combined" / "scores.json")
            self.assertEqual(scores["grading"]["judge_calls"], 3)
            self.assertEqual(scores["grading"]["usage_total"]["input_tokens"], 2700)
            for row in scores["rows"]:
                self.assertFalse(row["confirmed_business_success"])
                self.assertIsNone(row["review"])
                self.assertEqual(row["usage"]["input_tokens"], 20)
            prepared = prepare_from_files(
                root / "combined" / "scores.json", ROOT / "configs" / "examples" / "cost-evaluation.json",
                root / "cost-input.json")
            self.assertEqual(prepared["evaluation_bridge"]["model_grading"]["judge_calls"], 3)
            self.assertIn("Model grading is provisional", " ".join(prepared["evaluation_bridge"]["hold_reasons"]))
            for variant in prepared["variants"].values():
                self.assertEqual(variant["usage_per_request"]["input_tokens"], 20)
                self.assertFalse(variant["quality"]["review_complete"])
                self.assertIsNone(variant["quality"]["successful_requests"])


if __name__ == "__main__":
    unittest.main()
