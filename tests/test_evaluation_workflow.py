from copy import deepcopy
from contextlib import redirect_stderr
import csv
import importlib.util
from io import StringIO
import json
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from foundry_distillation_lab.evaluation import evaluate, evidence_sha256
from foundry_distillation_lab.evaluation.schema import ContractError
from foundry_distillation_lab.evaluation.workflow import combine_runs, compare_runs, review_run, review_sheet, run_evaluation
from foundry_distillation_lab.io import read_json, sha256


ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / "runs" / f"evaluation-workflow-test-{uuid.uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.bundle = read_json(ROOT / "data" / "samples" / "evaluation-next-action.json")
        self.response = {"message": deepcopy(self.bundle["records"][0]["message"]),
                         "response_model": "student-version-2026", "response_id": "chatcmpl-test",
                         "usage": {"input_tokens": 10, "output_tokens": 4, "cached_input_tokens": None}}
        self.bundle.update(records=[], models=["base", "fine_tuned"])
        self.bundle["source"] = {"kind": "local_test", "reference": '引用,\n"日本語"'}
        self.config = {
            "base_url": "https://example.invalid/openai/v1/",
            "targets": {"base": "student-base", "fine_tuned": "student-fine-tuned"},
            "max_completion_tokens": 1024, "timeout_seconds": 60,
            "max_model_calls": 12, "max_tool_calls": 24,
        }
        self.input_path, self.config_path = self.directory / "input.json", self.directory / "config.json"
        self.save_inputs()
        spec = importlib.util.spec_from_file_location("evaluation_workflow_cli", ROOT / "scripts" / "evaluate.py")
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def tearDown(self):
        for attempt in range(10):
            try:
                shutil.rmtree(self.directory)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1)

    def save_inputs(self):
        self.input_path.write_text(json.dumps(self.bundle, ensure_ascii=False), encoding="utf-8")
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def run_model(self, model="base", name=None, invoke=None):
        run_dir = self.directory / (name or model)
        report, complete = run_evaluation(
            self.input_path, self.config_path, "next-action", model, run_dir,
            invoke=invoke if invoke is not None else Mock(return_value=deepcopy(self.response)))
        return run_dir, report, complete

    def edit_reviews(self, run_dir, **values):
        path = run_dir / "reviews.csv"
        if not path.exists():
            review_sheet(run_dir)
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields, rows = reader.fieldnames, list(reader)
        for row in rows:
            row.update(values)
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def reviewed(self, run_dir, decision, name):
        self.edit_reviews(run_dir, decision=decision, reviewer="reviewer", notes='確認済み,\n"要約"')
        output = self.directory / name
        review_run(run_dir, output)
        return output

    def test_run_frozen_artifacts_identity_and_no_probe_or_oracle(self):
        original = deepcopy(self.bundle)
        invoke = Mock(return_value=self.response)
        directory, report, complete = self.run_model(invoke=invoke)
        self.assertTrue(complete)
        self.assertEqual(report["scheduled_slots"], 1)
        self.assertEqual(report["observed_records"], 1)
        self.assertEqual(report["rows"][0]["status"], "review_pending")
        self.assertEqual(report["overall"]["confirmed_business_successes"], 0)
        invoke.assert_called_once()
        request = invoke.call_args.args[0]
        self.assertEqual(set(request), {"attempt_id", "model_label", "target", "messages", "tools"})
        self.assertEqual(request["target"], "student-base")
        self.assertEqual(read_json(directory / "input.json"), original)
        evidence = read_json(directory / "evidence.json")
        self.assertEqual(evidence["models"], ["base"])
        self.assertEqual(evidence["evidence_kind"], "live_local_direct_model")
        for key in ("cases", "tools", "source"):
            self.assertEqual(evidence[key], original[key])
        record = evidence["records"][0]
        self.assertEqual(record["response_model"], "student-version-2026")
        self.assertEqual(record["response_id"], "chatcmpl-test")
        self.assertEqual(record["provenance"]["input_sha256"], sha256(self.input_path))
        self.assertNotIn("approved", json.dumps(record["provenance"]))
        self.assertEqual(report["per_model"]["base"]["runtime_provenance"]["classification"], "local_direct_model")
        self.assertFalse((directory / "reviews.csv").exists())
        for name in ("scores.json", "execution-start.json", "execution.json", "record-000001.json"):
            self.assertTrue((directory / name).is_file())
        execution = read_json(directory / "execution.json")
        self.assertEqual(execution["input_sha256"], sha256(directory / "input.json"))
        with self.assertRaises(FileExistsError):
            self.run_model(invoke=invoke)
        invoke.assert_called_once()

    def test_failure_stops_without_retry_preserves_denominator_and_unknown_usage(self):
        second = deepcopy(self.bundle["cases"][0])
        second["case_id"] = "second-case"
        self.bundle["cases"].append(second)
        self.save_inputs()
        invoke = Mock(side_effect=TimeoutError)
        directory, report, complete = self.run_model(invoke=invoke)
        invoke.assert_called_once()
        self.assertFalse(complete)
        self.assertEqual(report["scheduled_slots"], 2)
        self.assertEqual(report["observed_records"], 1)
        self.assertEqual(report["overall"]["status_counts"]["technical_failure"], 2)
        self.assertIsNone(report["overall"]["usage_total"]["input_tokens"])
        record = read_json(directory / "evidence.json")["records"][0]
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(record["error_type"], "TimeoutError")
        self.assertIsNone(record["usage"]["output_tokens"])
        self.assertEqual(report["rows"][1]["technical_failures"], ["missing_record"])

    def test_original_input_digest_does_not_follow_midflight_file_changes(self):
        digest = sha256(self.input_path)
        def invoke(payload):
            self.input_path.write_text("{}", encoding="utf-8")
            return self.response
        directory, _, _ = self.run_model(invoke=invoke)
        execution = read_json(directory / "execution.json")
        record = read_json(directory / "evidence.json")["records"][0]
        self.assertEqual(execution["source_input_sha256"], digest)
        self.assertEqual(record["provenance"]["input_sha256"], digest)
        self.assertEqual(read_json(directory / "input.json"), self.bundle)

    def test_execution_config_is_durable_before_first_request_and_survives_interrupt(self):
        output = self.directory / "interrupted"
        def invoke(payload):
            initial = read_json(output / "execution-start.json")
            self.assertEqual(initial["mode"], "next-action")
            self.assertEqual(initial["model"], "base")
            self.assertEqual(initial["target"], "student-base")
            self.assertEqual(initial["config"], self.config)
            self.assertEqual(initial["source_input_sha256"], sha256(self.input_path))
            self.assertTrue(initial["started_at"])
            self.assertNotIn("complete", initial)
            self.assertTrue((output / "input.json").is_file())
            self.assertTrue((output / "attempts" / f"{payload['attempt_id']}.started.json").is_file())
            raise KeyboardInterrupt
        transport = Mock(side_effect=invoke)
        with self.assertRaises(KeyboardInterrupt):
            self.run_model(name="interrupted", invoke=transport)
        transport.assert_called_once()
        self.assertEqual(read_json(output / "execution-start.json")["config"], self.config)
        self.assertEqual(len(list((output / "attempts").glob("*.started.json"))), 1)
        self.assertEqual(list((output / "attempts").glob("*.result.json")), [])
        self.assertFalse((output / "execution.json").exists())
        self.assertFalse((output / "evidence.json").exists())
        self.assertFalse((output / "scores.json").exists())
        with self.assertRaises(FileExistsError):
            self.run_model(name="interrupted", invoke=transport)
        transport.assert_called_once()

    def test_late_failure_keeps_first_normal_case_and_unknown_total(self):
        second = deepcopy(self.bundle["cases"][0])
        second["case_id"] = "second"
        self.bundle["cases"].append(second)
        self.save_inputs()
        _, report, complete = self.run_model(invoke=Mock(side_effect=[self.response, TimeoutError()]))
        self.assertFalse(complete)
        self.assertEqual(report["observed_records"], 2)
        self.assertEqual(report["rows"][0]["status"], "review_pending")
        self.assertIsNone(report["overall"]["usage_total"]["input_tokens"])
        self.assertEqual(report["overall"]["usage_known_subtotal"]["input_tokens"], 10)

    def test_all_validation_precedes_first_request(self):
        original = deepcopy(self.bundle)
        mutations = [
            lambda bundle: bundle["cases"][1]["expected"].update(kind="bogus"),
            lambda bundle: bundle["cases"][1].update(messages=[]),
            lambda bundle: bundle["cases"][1].update(messages=[{"role": "invalid", "content": "bad"}]),
            lambda bundle: bundle["cases"][1].update(messages=[{"role": "user", "content": {"bad": 1}}]),
            lambda bundle: bundle["cases"][1].update(messages=[{"role": "tool", "content": "x", "tool_call_id": "unknown"}]),
            lambda bundle: bundle["cases"][1].update(ground_truth=[]),
            lambda bundle: bundle["cases"][1].update(reference={"tool_calls": []}),
            lambda bundle: bundle["cases"][1].update(kind="text"),
            lambda bundle: bundle.update(schema="unknown"),
            lambda bundle: bundle.update(models=["fine_tuned"]),
            lambda bundle: bundle.update(inference={"base_url": "ignored"}),
            lambda bundle: bundle["cases"][1].update(case_id=bundle["cases"][0]["case_id"]),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                self.bundle = deepcopy(original)
                self.bundle["cases"].append(deepcopy(self.bundle["cases"][0]))
                self.bundle["cases"][1]["case_id"] = "later"
                mutate(self.bundle)
                self.save_inputs()
                invoke = Mock()
                with self.assertRaises(ValueError):
                    self.run_model(name=f"invalid-{index}", invoke=invoke)
                invoke.assert_not_called()
                self.assertFalse((self.directory / f"invalid-{index}").exists())

    def test_config_validation_before_request(self):
        original = deepcopy(self.config)
        mutations = [
            {"reserve_per_request": 1},
            {"max_model_calls": False}, {"max_tool_calls": 0},
            {"max_completion_tokens": -1}, {"timeout_seconds": 601},
            {"timeout_seconds": True}, {"timeout_seconds": float("nan")},
            {"base_url": "http://example.invalid/openai/v1/"},
            {"base_url": "https://example.invalid/not-v1/"},
            {"base_url": "https://user:password@example.invalid/openai/v1/"},
            {"targets": {"fine_tuned": "only-other-model"}},
            {"targets": {"base": ""}},
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                self.config = {**original, **mutation}
                self.save_inputs()
                invoke = Mock()
                with self.assertRaises(ValueError):
                    self.run_model(name=f"bad-config-{index}", invoke=invoke)
                invoke.assert_not_called()

    def test_bad_message_sequence_is_rejected_before_request(self):
        call = self.response["message"]["tool_calls"][0]
        self.bundle["cases"][0]["messages"].extend([
            {"role": "assistant", "tool_calls": [call]},
            {"role": "user", "content": "tool result missing"},
        ])
        self.save_inputs()
        invoke = Mock()
        with self.assertRaisesRegex(ContractError, "missing_input_tool_results"):
            self.run_model(invoke=invoke)
        invoke.assert_not_called()

    def test_valid_historical_tool_messages_and_prepared_references(self):
        call = self.response["message"]["tool_calls"][0]
        case = self.bundle["cases"][0]
        case["messages"].extend([
            {"role": "assistant", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call["id"], "content": '{"order_id":"ORD-DEMO-001"}'},
        ])
        case["reference"] = {"role": "assistant", "tool_calls": [call]}
        case["ground_truth"] = deepcopy(case["expected"]["calls"])
        case["kind"] = "tool"
        self.save_inputs()
        _, report, complete = self.run_model()
        self.assertTrue(complete)
        self.assertEqual(report["rows"][0]["status"], "review_pending")

    def test_empty_text_reference_is_rejected_before_network(self):
        self.bundle["cases"][0].update(expected={"kind": "text", "calls": []},
                                      reference={"role": "assistant", "content": " "})
        self.save_inputs()
        invoke = Mock()
        with self.assertRaisesRegex(ContractError, "text_reference_requires_content"):
            self.run_model(invoke=invoke)
        invoke.assert_not_called()

    def test_live_e2e_validates_contract_and_records_bounded_execution(self):
        from foundry_distillation_lab.retail import RetailSession
        session = RetailSession()
        self.bundle = {
            "schema": "retail-evaluation-input-v1", "models": ["base", "fine_tuned"],
            "tools": session.tools, "records": [],
            "cases": [{"case_id": "answer-only", "user_input": "こんにちは",
                       "system_prompt": session.system_prompt,
                       "expected": {"initial_tools": [], "required_calls": [],
                                    "allowed_mutations": [],
                                    "final_state": {"terminal": "answer_only", "submissions": []}}}],
        }
        self.save_inputs()
        invoke = Mock(return_value={"message": {"content": "こんにちは。"}, "response_id": "e2e-response"})
        output = self.directory / "e2e"
        report, complete = run_evaluation(self.input_path, self.config_path, "e2e", "base", output, invoke=invoke)
        self.assertTrue(complete)
        self.assertEqual(report["rows"][0]["status"], "review_pending")
        record = read_json(output / "evidence.json")["records"][0]
        self.assertEqual(record["events"][1]["response_id"], "e2e-response")
        self.assertEqual(record["model_calls"], 1)
        self.assertIsNone(record["usage"]["input_tokens"])
        review_sheet(output)
        with (output / "reviews.csv").open(encoding="utf-8-sig", newline="") as stream:
            review = next(csv.DictReader(stream))
        self.assertEqual(review["reference"], "null")
        self.assertEqual(review["category"], "null")
        self.bundle["cases"][0]["system_prompt"] = "not the session prompt"
        self.save_inputs()
        unused = Mock()
        with self.assertRaisesRegex(ContractError, "retail_contract_differs"):
            run_evaluation(self.input_path, self.config_path, "e2e", "base",
                           self.directory / "invalid-e2e", invoke=unused)
        unused.assert_not_called()

    def test_malformed_model_reply_fails_fast_even_if_transport_returned(self):
        self.bundle["cases"].append({**deepcopy(self.bundle["cases"][0]), "case_id": "second"})
        self.save_inputs()
        invoke = Mock(return_value={"message": {"content": []}, "response_id": "bad-response"})
        directory, report, complete = self.run_model(invoke=invoke)
        self.assertFalse(complete)
        invoke.assert_called_once()
        self.assertEqual(report["scheduled_slots"], 2)
        self.assertEqual(read_json(directory / "evidence.json")["records"][0]["response_id"], "bad-response")

    def test_review_exact_scorer_schema_auto_timestamp_and_immutable_original(self):
        directory, _, _ = self.run_model()
        original = (directory / "evidence.json").read_bytes()
        reviewed = self.reviewed(directory, "success", "reviewed")
        evidence = read_json(reviewed / "evidence.json")
        record = evidence["records"][0]
        self.assertEqual(set(record["review"]), {"decision", "reviewer", "notes", "reviewed_at", "evidence_sha256"})
        self.assertEqual(record["review"]["decision"], "confirmed_success")
        self.assertEqual(record["review"]["evidence_sha256"], evidence_sha256(record))
        self.assertTrue(record["review"]["reviewed_at"])
        self.assertEqual(read_json(reviewed / "scores.json")["rows"][0]["status"], "confirmed_success")
        self.assertEqual((directory / "evidence.json").read_bytes(), original)
        self.assertTrue((reviewed / "input.json").is_file())
        self.assertTrue((reviewed / "review-input.csv").is_file())
        self.assertTrue((reviewed / "review-input.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_reference_and_category_are_readable_bound_csv_and_comparison_fields(self):
        reference = {"role": "assistant", "content": '参照例です。\n"注文", 配送状況を確認します。'}
        category = "日本語の案内"
        self.bundle["cases"][0].update(expected={"kind": "text", "calls": []},
                                      reference=reference, category=category)
        self.response["message"] = {"content": "配送状況を確認します。"}
        self.save_inputs()
        before, _, _ = self.run_model()
        after, _, _ = self.run_model("fine_tuned")
        review_sheet(before)
        with (before / "reviews.csv").open(encoding="utf-8-sig", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(json.loads(row["reference"]), reference)
        self.assertEqual(json.loads(row["category"]), category)
        self.assertTrue((before / "reviews.csv").read_bytes().startswith(b"\xef\xbb\xbf"))
        output = self.directory / "reference-comparison"
        comparison = compare_runs(before, after, output)
        self.assertEqual(comparison["cases"][0]["reference"], reference)
        self.assertEqual(comparison["cases"][0]["category"], category)
        compared = read_json(output / "comparison.json")["cases"][0]
        self.assertEqual(compared["reference"], reference)
        self.assertEqual(compared["category"], category)
        self.assertTrue((output / "comparison.md").is_file())
        self.assertTrue((output / "comparison.csv").read_bytes().startswith(b"\xef\xbb\xbf"))
        for field in ("reference", "category"):
            self.edit_reviews(before, **{field: '"changed"'})
            with self.assertRaisesRegex(ContractError, "stale_or_changed_review_evidence"):
                review_run(before, self.directory / f"changed-{field}")
            self.edit_reviews(before, **{field: row[field]})

    def test_blank_and_needs_review_remain_pending(self):
        directory, _, _ = self.run_model()
        for index, decision in enumerate(("", "needs_review")):
            self.edit_reviews(directory, decision=decision)
            report = review_run(directory, self.directory / f"reviewed-{index}")
            self.assertEqual(report["rows"][0]["status"], "review_pending")
            self.assertEqual(report["overall"]["confirmed_success_rate"], 0)
        self.edit_reviews(directory, reviewer="somebody")
        # A needs_review annotation is allowed without a confirmed review.
        report = review_run(directory, self.directory / "not-confirmed")
        self.assertEqual(report["rows"][0]["status"], "review_pending")
        self.edit_reviews(directory, decision="")
        with self.assertRaisesRegex(ContractError, "review_decision_required"):
            review_run(directory, self.directory / "blank-with-metadata")

    def test_stale_review_and_changed_identity_rejected(self):
        directory, _, _ = self.run_model()
        self.edit_reviews(directory, evidence_sha256="0" * 64, decision="success", reviewer="r", notes="n")
        with self.assertRaisesRegex(ContractError, "stale_or_changed"):
            review_run(directory, self.directory / "bad")
        self.edit_reviews(directory, case_id="other")
        with self.assertRaisesRegex(ContractError, "identity_mismatch"):
            review_run(directory, self.directory / "bad-identity")

    def test_review_requires_decision_reviewer_notes_and_exact_digest(self):
        directory, _, _ = self.run_model()
        for index, fields in enumerate((
                {"decision": "confirmed_success", "reviewer": "r", "notes": "n"},
                {"decision": "success", "reviewer": "", "notes": "n"},
                {"decision": "failure", "reviewer": "r", "notes": ""})):
            with self.subTest(fields=fields):
                self.edit_reviews(directory, **fields)
                with self.assertRaises(ContractError):
                    review_run(directory, self.directory / f"invalid-review-{index}")

    def test_human_success_cannot_override_deterministic_failure(self):
        response = deepcopy(self.response)
        response["message"]["tool_calls"][0]["function"]["arguments"] = '{"order_id":"WRONG"}'
        directory, report, _ = self.run_model(invoke=Mock(return_value=response))
        self.assertEqual(report["rows"][0]["status"], "quality_failure")
        reviewed = self.reviewed(directory, "success", "reviewed")
        scored = read_json(reviewed / "scores.json")
        self.assertEqual(scored["rows"][0]["status"], "quality_failure")
        self.assertEqual(scored["overall"]["confirmed_success_rate"], 0)

    def test_success_review_cannot_override_technical_failure(self):
        directory, _, _ = self.run_model(invoke=Mock(side_effect=TimeoutError))
        reviewed = self.reviewed(directory, "success", "reviewed")
        self.assertEqual(read_json(reviewed / "scores.json")["rows"][0]["status"], "technical_failure")

    def test_compare_pending_is_incomparable_and_json_roundtrips(self):
        before, _, _ = self.run_model()
        after, _, _ = self.run_model("fine_tuned")
        output = self.directory / "comparison"
        report = compare_runs(before, after, output)
        self.assertEqual(report["decision"], "incomparable")
        self.assertEqual(report["cases"][0]["decision"], "incomparable")
        rows = read_json(output / "comparison.json")["cases"]
        self.assertEqual(rows[0]["before"]["model"], "base")
        self.assertEqual(rows[0]["after"]["target"], "student-fine-tuned")
        self.assertEqual(rows[0]["messages"], self.bundle["cases"][0]["messages"])
        self.assertEqual(rows[0]["expected"], self.bundle["cases"][0]["expected"])
        self.assertEqual(rows[0]["before"]["response"], self.response["message"])
        with self.assertRaises(FileExistsError):
            compare_runs(before, after, output)

    def test_comparison_decisions_use_reviewed_results_only(self):
        before, _, _ = self.run_model()
        after, _, _ = self.run_model("fine_tuned")
        failed = self.reviewed(before, "failure", "failed")
        passed = self.reviewed(after, "success", "passed")
        for index, (left, right, expected) in enumerate((
                (failed, passed, "improvement"), (passed, failed, "regression"),
                (passed, passed, "no_change"), (failed, failed, "no_change"),
                (before, passed, "incomparable"))):
            report = compare_runs(left, right, self.directory / f"compare-{index}")
            self.assertEqual(report["decision"], expected)
            self.assertEqual(report["cases"][0]["decision"], expected)
        row = read_json(self.directory / "compare-0" / "comparison.json")["cases"][0]
        self.assertEqual(row["after"]["review"]["notes"], '確認済み,\n"要約"')

    def test_compare_refuses_case_reference_source_tools_config_mismatches(self):
        before, _, _ = self.run_model()
        original = deepcopy(self.bundle)
        mutations = [
            lambda: self.bundle["cases"][0].update(case_id="different"),
            lambda: self.bundle["cases"][0]["expected"]["calls"][0]["arguments"].update(order_id="OTHER"),
            lambda: self.bundle["source"].update(reference="different"),
            lambda: self.bundle["tools"][0]["function"].update(description="different"),
            lambda: self.bundle["cases"][0]["messages"][0].update(content="different"),
            lambda: self.config.update(max_completion_tokens=42),
            lambda: self.config.update(timeout_seconds=30),
            lambda: self.config.update(max_model_calls=4),
            lambda: self.config.update(base_url="https://other.invalid/openai/v1/"),
        ]
        initial_config = deepcopy(self.config)
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                self.bundle, self.config = deepcopy(original), deepcopy(initial_config)
                mutation()
                self.save_inputs()
                after, _, _ = self.run_model("fine_tuned", f"after-{index}")
                with self.assertRaisesRegex(ContractError, "comparison_.*_mismatch"):
                    compare_runs(before, after, self.directory / f"comparison-{index}")

    def test_changed_frozen_evidence_or_input_is_rejected(self):
        before, _, _ = self.run_model()
        after, _, _ = self.run_model("fine_tuned")
        for name in ("input", "evidence"):
            path = after / f"{name}.json"
            saved = path.read_bytes()
            path.write_bytes(saved + b" ")
            with self.assertRaisesRegex(ContractError, f"changed_{name}_snapshot"):
                compare_runs(before, after, self.directory / f"bad-{name}")
            path.write_bytes(saved)

    def test_combine_preserves_reviews_and_enables_e2e_cost_projection(self):
        from foundry_distillation_lab.reporting.bridge import prepare_cost_input
        from foundry_distillation_lab.retail import RetailSession
        session = RetailSession()
        models = ["teacher", "base", "fine_tuned"]
        source = {"kind": "development_cases", "evidence_kind": "unreviewed_cases_no_predictions",
                  "quality_review_approved": False}
        self.bundle = {
            "schema": "retail-evaluation-input-v1", "models": models, "source": source,
            "evidence_kind": "unreviewed_cases_no_predictions", "tools": session.tools, "records": [],
            "cases": [{"case_id": "answer-only", "user_input": "こんにちは",
                       "system_prompt": session.system_prompt,
                       "expected": {"initial_tools": [], "required_calls": [],
                                    "allowed_mutations": [],
                                    "final_state": {"terminal": "answer_only", "submissions": []}}}],
        }
        self.config["targets"]["teacher"] = "teacher-deployment"
        self.save_inputs()
        reviewed_runs, original_records = [], []
        for model in models:
            run_dir = self.directory / model
            response = {"message": {"content": "こんにちは。"}, "response_model": "reported-version",
                        "response_id": f"response-{model}",
                        "usage": {"input_tokens": 10, "output_tokens": 3, "cached_input_tokens": 0}}
            _, complete = run_evaluation(self.input_path, self.config_path, "e2e", model, run_dir,
                                         invoke=Mock(return_value=response))
            self.assertTrue(complete)
            reviewed = self.reviewed(run_dir, "success", f"{model}-reviewed")
            reviewed_runs.append(reviewed)
            original_records.extend(read_json(reviewed / "evidence.json")["records"])
        combined = self.directory / "combined"
        self.assertEqual(self.cli.main(["combine", "--run-dirs", *map(str, reviewed_runs),
                                       "--output-dir", str(combined)]), 0)
        evidence = read_json(combined / "evidence.json")
        scores = read_json(combined / "scores.json")
        self.assertEqual(evidence["records"], original_records)
        self.assertEqual(evidence["source"], source)
        self.assertEqual(read_json(combined / "input.json")["source"], source)
        self.assertEqual(evidence["evidence_kind"], "live_local_direct_model")
        self.assertEqual(len(evidence["cases"]), 1)
        self.assertEqual(evidence["models"], models)
        self.assertEqual(scores["overall"]["confirmed_business_successes"], 3)
        self.assertEqual(scores["overall"]["runtime_provenance"]["classification"], "local_direct_model")
        for record in evidence["records"]:
            self.assertEqual(record["review"]["evidence_sha256"], evidence_sha256(record))
            self.assertEqual(record["provenance"]["evidence_kind"], "live_local_direct_model")
        metadata = read_json(combined / "execution.json")
        self.assertEqual(metadata["mode"], "e2e")
        self.assertTrue(metadata["complete"])
        self.assertEqual(metadata["config"]["targets"], self.config["targets"])
        self.assertEqual(metadata["evidence_sha256"], sha256(combined / "evidence.json"))
        config = read_json(ROOT / "configs" / "examples" / "cost-evaluation.json")
        cost_input = prepare_cost_input(scores, config, sha256(combined / "scores.json"))
        self.assertEqual(cost_input["evidence_kind"], "actual")
        for variant in cost_input["variants"].values():
            self.assertTrue(variant["quality"]["review_complete"])
            self.assertEqual(variant["quality"]["successful_requests"], 1)
        self.assertEqual(evaluate(evidence, "e2e"), scores)
        with self.assertRaises(FileExistsError):
            combine_runs(reviewed_runs, combined)

    def test_combine_rejects_duplicate_models_and_mismatched_contracts(self):
        base, _, _ = self.run_model()
        with self.assertRaisesRegex(ContractError, "unique_model_labels"):
            combine_runs([base, base], self.directory / "duplicate")
        with self.assertRaisesRegex(ContractError, "at_least_two"):
            combine_runs([base], self.directory / "one")
        original_bundle, original_config = deepcopy(self.bundle), deepcopy(self.config)
        changes = [
            lambda: self.bundle["cases"][0]["messages"][0].update(content="other"),
            lambda: self.bundle["cases"][0]["expected"]["calls"][0]["arguments"].update(order_id="OTHER"),
            lambda: self.bundle["source"].update(reference="other"),
            lambda: self.bundle["tools"][0]["function"].update(description="other"),
            lambda: self.config.update(max_completion_tokens=512),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                self.bundle, self.config = deepcopy(original_bundle), deepcopy(original_config)
                change()
                self.save_inputs()
                after, _, _ = self.run_model("fine_tuned", f"changed-{index}")
                output = self.directory / f"incompatible-{index}"
                with self.assertRaisesRegex(ContractError, "comparison_.*_mismatch"):
                    combine_runs([base, after], output)
                self.assertFalse(output.exists())

    def test_combine_keeps_failed_slots_and_does_not_promote_pending_reviews(self):
        base, _, _ = self.run_model()
        failed, _, _ = self.run_model("fine_tuned", invoke=Mock(side_effect=TimeoutError))
        output = self.directory / "combined"
        scores = combine_runs([base, failed], output)
        self.assertEqual(scores["scheduled_slots"], 2)
        self.assertEqual(scores["observed_records"], 2)
        self.assertEqual(scores["overall"]["status_counts"]["review_pending"], 1)
        self.assertEqual(scores["overall"]["status_counts"]["technical_failure"], 1)
        self.assertEqual(scores["overall"]["confirmed_success_rate"], 0)
        self.assertIsNone(scores["overall"]["usage_total"]["input_tokens"])
        self.assertFalse(read_json(output / "execution.json")["complete"])

    def test_cli_run_review_compare_and_offline_rescore(self):
        args = ["run", "--mode", "next-action", "--input", str(self.input_path),
                "--config", str(self.config_path), "--model", "base",
                "--run-dir", str(self.directory / "cli-base")]
        real_run = run_evaluation
        def injected(*arguments):
            return real_run(*arguments, invoke=Mock(return_value=self.response))
        with patch.object(self.cli, "run_evaluation", side_effect=injected):
            self.assertEqual(self.cli.main(args), 0)
        base = self.directory / "cli-base"
        self.edit_reviews(base, decision="success", reviewer="r", notes="n")
        reviewed = self.directory / "cli-reviewed"
        self.assertEqual(self.cli.main(["review", "--run-dir", str(base), "--output-dir", str(reviewed)]), 0)
        self.assertEqual(self.cli.main(["compare", "--before", str(base), "--after", str(reviewed),
                                       "--output-dir", str(self.directory / "cli-compare")]), 0)
        for prefix in ([], ["score"]):
            output = self.directory / ("legacy-score.json" if not prefix else "score.json")
            self.assertEqual(self.cli.main(prefix + ["--mode", "next-action", "--input",
                                                    str(reviewed / "evidence.json"), "--output", str(output)]), 0)
            self.assertEqual(read_json(output), read_json(reviewed / "scores.json"))

    def test_cli_incomplete_returns_nonzero_and_keeps_evidence(self):
        def injected(*arguments):
            return run_evaluation(*arguments, invoke=Mock(side_effect=TimeoutError))
        with patch.object(self.cli, "run_evaluation", side_effect=injected):
            code = self.cli.main(["run", "--mode", "next-action", "--input", str(self.input_path),
                                  "--config", str(self.config_path), "--model", "base",
                                  "--run-dir", str(self.directory / "incomplete")])
        self.assertEqual(code, 2)
        self.assertTrue((self.directory / "incomplete" / "scores.json").is_file())

    def test_cli_removed_flags_are_argparse_errors(self):
        for flag in ("--send", "--approval"):
            stderr = StringIO()
            with self.subTest(flag=flag), redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                self.cli.main(["--mode", "next-action", "--input", str(self.input_path),
                               "--output", str(self.directory / "never.json"), flag])
            self.assertEqual(error.exception.code, 2)
            self.assertIn(f"unrecognized arguments: {flag}", stderr.getvalue())

    def test_existing_deterministic_fixture_scoring_is_unchanged(self):
        statuses = {
            "next-action": ["review_pending", "quality_failure", "review_pending"],
            "e2e": ["review_pending", "technical_failure", "technical_failure"],
        }
        for mode in ("next-action", "e2e"):
            bundle = read_json(ROOT / "data" / "samples" / f"evaluation-{mode}.json")
            report = evaluate(bundle, mode)
            self.assertEqual(report["schema"], "retail-evaluation-report-v1")
            self.assertEqual(report["scheduled_slots"], 3)
            self.assertEqual(report["overall"]["confirmed_business_successes"], 0)
            self.assertEqual([row["status"] for row in report["rows"]], statuses[mode])
            self.assertEqual(report["overall"]["runtime_provenance"]["classification"],
                             "synthetic" if mode == "next-action" else "mixed")


if __name__ == "__main__":
    unittest.main()
