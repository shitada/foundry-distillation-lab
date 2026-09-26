"""Independent business goldens and offline execution of the curated benchmark."""

from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

from foundry_distillation_lab.datasets.prepare import prepare
from foundry_distillation_lab.evaluation.cases import (
    CANDIDATES_PER_SCENARIO, PERIOD, SCENARIOS, _case, prepare_cases,
)
from foundry_distillation_lab.evaluation.runner import run_case
from foundry_distillation_lab.evaluation.scoring import score_e2e
from foundry_distillation_lab.evaluation.workflow import _validate
from foundry_distillation_lab.io import canonical, read_json, read_jsonl, sha256
from foundry_distillation_lab.retail import RetailSession


ROOT = Path(__file__).resolve().parents[1]
CONFIG = {"base_url": "https://offline.example/openai/v1/",
          "targets": {"teacher": "teacher", "base": "base", "fine_tuned": "fine_tuned"},
          "max_completion_tokens": 2048, "timeout_seconds": 60,
          "max_model_calls": 10, "max_tool_calls": 10}

# Independent from case templates and store arithmetic: refund, fee, credit, difference.
AMOUNTS = {
    "refund_standard": (10965, 1935, 0, 0),
    "refund_gold": (12025, 975, 0, 0),
    "refund_platinum": (6800, 0, 0, 0),
    "exchange": (0, 0, 0, 100),
    "cancellation": (4500, 0, 0, 0),
    "lost_delivery": (24900, 0, 0, 0),
    "late_credit": (0, 0, 1000, 0),
    "damaged_final_sale": (0, 0, 13000, 0),
}
POLICY = {
    "refund_standard": ("お客様都合", "返金"),
    "refund_gold": ("お客様都合", "返金"),
    "refund_platinum": ("お客様都合", "返金"),
    "exchange": ("サイズ違い", "交換"),
    "cancellation": ("お客様都合", "キャンセル"),
    "expired_return": ("お客様都合", None),
    "lost_delivery": ("未着", "返金"),
    "late_credit": ("遅配", "配送遅延クレジット"),
    "damaged_final_sale": ("破損", "ストアクレジット"),
}


def scripted_model(case, captured):
    """Test-only plan; IDs come from live tools, never expected/calculated oracle."""
    category = case["category"]
    order_match = re.search(r"ORD-\d+", case["user_input"])
    item_match = re.search(r"ITEM-\d+-\d+", case["user_input"])
    order = order_match.group() if order_match else None
    item = item_match.group() if item_match else None
    steps = [] if category == "missing_order" else ["get_order_details", "get_fulfillment_status"]
    if category in POLICY:
        steps += ["check_resolution_policy"]
        if category == "exchange":
            steps += ["check_inventory"]
        if POLICY[category][1]:
            steps += ["calculate_resolution", "submit_resolution"]
    index = 0

    def invoke(payload):
        nonlocal index
        captured.append(deepcopy(payload))
        if index >= len(steps):
            return {"message": {"role": "assistant", "content": "必要な確認を行いました。実システムは変更していません。"},
                    "usage": {"input_tokens": 10, "output_tokens": 10, "cached_input_tokens": 0}}
        name = steps[index]
        index += 1
        arguments = {"order_id": order}
        outputs = [json.loads(message["content"]) for message in payload["messages"]
                   if message["role"] == "tool"]
        if name == "check_resolution_policy":
            arguments.update(item_id=item, reason=POLICY[category][0])
        elif name == "check_inventory":
            arguments = {"sku": "SKU-002-3"}
        elif name == "calculate_resolution":
            policy = next(result for result in outputs if "policy_id" in result)
            requested = {"item_id": item, "actions": [POLICY[category][1]],
                         "reason": POLICY[category][0], "policy_id": policy["policy_id"]}
            if category == "exchange":
                inventory = next(result for result in outputs if "inventory_check_id" in result)
                requested.update(replacement_sku="SKU-002-3",
                                 inventory_check_id=inventory["inventory_check_id"])
            arguments["items"] = [requested]
        elif name == "submit_resolution":
            calculation = next(result for result in outputs if "calculation_id" in result)
            arguments.update(calculation_id=calculation["calculation_id"],
                             resolution_summary="確認済みの金額と対応でシミュレーションを確定します。")
        return {"message": {"role": "assistant", "tool_calls": [
            {"id": f"step-{index}", "type": "function",
             "function": {"name": name, "arguments": canonical(arguments)}}]},
                "usage": {"input_tokens": 10, "output_tokens": 10, "cached_input_tokens": 0}}
    return invoke


class EvaluationCasesTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / ".test-runs" / f"evaluation-cases-{uuid.uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.data = self.directory / "data"
        prepare(ROOT / "data" / "samples" / "traces.jsonl", self.data)
        self.output = self.directory / "input.json"

    def bundle(self):
        return prepare_cases(self.data, self.output)

    def rewrite(self, name, value, jsonl=False):
        path = self.data / name
        text = "".join(canonical(row) + "\n" for row in value) if jsonl else canonical(value)
        path.write_text(text, encoding="utf-8")
        if jsonl:
            manifest = read_json(self.data / "manifest.json")
            manifest["output_hashes"][name] = sha256(path)
            (self.data / "manifest.json").write_text(canonical(manifest), encoding="utf-8")

    def test_prepare_never_executes_store_or_needs_network(self):
        with patch.object(RetailSession, "call", side_effect=AssertionError("oracle execution forbidden")):
            bundle = self.bundle()
            for model in bundle["models"]:
                _validate(bundle, CONFIG, "e2e", model)
        self.assertEqual(bundle, read_json(self.output))
        self.assertEqual(len(bundle["cases"]), 11)
        self.assertEqual(bundle["records"], [])
        self.assertEqual(bundle["models"], ["teacher", "base", "fine_tuned"])
        self.assertEqual(bundle["benchmark_split"], "development")
        self.assertNotIn("inference", bundle)
        self.assertEqual(set(AMOUNTS), {c["category"] for c in bundle["cases"]
                                      if c["expected"]["allowed_mutations"]})
        for case in bundle["cases"]:
            self.assertEqual(case["system_prompt"], RetailSession().system_prompt)
            self.assertEqual(case["context"]["benchmark_split"], "development")
            self.assertTrue(case["reference"]["content"])

    def test_all_cases_and_periodic_alternatives_pass_independent_goldens(self):
        bundle = self.bundle()
        cases = list(bundle["cases"])
        for offset in range(1, CANDIDATES_PER_SCENARIO):
            cases.extend(_case(s, s["base"] + PERIOD * offset, RetailSession()) for s in SCENARIOS)
        for case in cases:
            with self.subTest(case=case["case_id"], order=case["context"].get("order_id")):
                captured = []
                record = run_case(case, "teacher", scripted_model(case, captured), RetailSession)
                score = score_e2e(case, record, bundle["tools"], "teacher")
                self.assertTrue(score["deterministic_checks_passed"], score)
                self.assertEqual(score["status"], "review_pending")
                self.assertFalse(score["confirmed_business_success"])
                for payload in captured:
                    self.assertEqual(set(payload), {"attempt_id", "model_label", "messages", "tools"})
                    serialized = canonical(payload)
                    for forbidden in ("required_calls", "allowed_mutations", "source_hashes",
                                      "independent_policy_golden_v1", "selected_order_number"):
                        self.assertNotIn(forbidden, serialized)
                category = case["category"]
                calculations = [event["result"] for event in record["events"]
                                if event["event"] == "tool_finish"
                                and "total_refund_jpy" in event.get("result", {})]
                if category in AMOUNTS:
                    self.assertEqual(len(calculations), 1)
                    self.assertEqual(tuple(calculations[0][field] for field in (
                        "total_refund_jpy", "total_restocking_fee_jpy", "total_credit_jpy",
                        "total_exchange_difference_jpy")), AMOUNTS[category])
                else:
                    self.assertEqual(calculations, [])
                if category == "missing_order":
                    self.assertEqual(record["tool_calls"], 0)

    def test_independent_tiers_windows_and_expiry(self):
        cases = {case["category"]: case for case in self.bundle()["cases"]}
        for category, tier, window, fee in (
                ("refund_standard", "標準", 15, 0.15),
                ("refund_gold", "ゴールド", 30, 0.075),
                ("refund_platinum", "プラチナ", 45, 0.0)):
            case = cases[category]
            calls = case["expected"]["required_calls"]
            self.assertEqual(calls[0]["result"]["customer"]["loyalty_tier"], tier)
            self.assertEqual(case["context"]["return_window_days"], window)
            self.assertEqual(calls[2]["result"]["restocking_fee_rate"], fee)
        expiry = cases["expired_return"]["expected"]
        self.assertEqual(expiry["required_calls"][1]["result"]["days_since_delivery"], 29)
        self.assertEqual(expiry["required_calls"][2]["result"]["eligible_actions"], ["対象外"])
        self.assertFalse(expiry["required_calls"][2]["result"]["eligible"])
        self.assertEqual(expiry["allowed_mutations"], [])
        self.assertEqual(cases["late_credit"]["context"]["return_window_days"], 60)

    def test_wrong_expectation_and_unauthorized_authority_fail(self):
        bundle = self.bundle()
        case = next(c for c in bundle["cases"] if c["category"] == "refund_gold")
        record = run_case(case, "teacher", scripted_model(case, []), RetailSession)
        changed = deepcopy(case)
        calculation = next(c for c in changed["expected"]["required_calls"]
                           if c["name"] == "calculate_resolution")
        calculation["result"]["total_refund_jpy"] = 13000
        self.assertFalse(score_e2e(changed, record, bundle["tools"], "teacher")["deterministic_checks_passed"])
        changed = deepcopy(case)
        mutation = changed["expected"]["allowed_mutations"][0]
        mutation["arguments"]["calculation_id"] = "CALC-WRONG"
        mutation["result"]["calculation_id"] = "CALC-WRONG"
        score = score_e2e(changed, record, bundle["tools"], "teacher")
        self.assertIn("executed_unauthorized_action", score["quality_failures"])

    def test_invalid_calls_and_tool_errors_never_pass(self):
        bundle = self.bundle()
        case = bundle["cases"][0]
        for name, arguments in (("unknown", {}), ("get_order_details", {"order_id": "invalid"})):
            with self.subTest(name=name):
                invoke = lambda payload: {"message": {"role": "assistant", "tool_calls": [
                    {"id": "bad", "name": name, "arguments": arguments}]}}
                record = run_case(case, "teacher", invoke, RetailSession)
                score = score_e2e(case, record, bundle["tools"], "teacher")
                self.assertFalse(score["deterministic_checks_passed"])
                self.assertFalse(score["confirmed_business_success"])

    def test_no_overlap_with_any_split_including_normalized_final(self):
        bundle = self.bundle()
        groups = bundle["provenance"]["excluded_order_groups"]
        self.assertEqual({g["order_number"] for g in groups}, set(range(1, 21)))
        selected = {choice["selected_order_number"] for choice in bundle["provenance"]["selection"]}
        self.assertEqual(len(selected), 10)
        self.assertFalse(selected & {g["order_number"] for g in groups})
        self.assertTrue(any("final.jsonl" in g["sources"] for g in groups))
        self.assertTrue(all("normalized.jsonl" in g["sources"] for g in groups))

    def inject_references(self, text):
        manifest = read_json(self.data / "manifest.json")
        normalized = read_jsonl(self.data / "normalized.jsonl")
        # Use a final conversation: exclusion must not be restricted to training/dev.
        key = manifest["partitions"]["final"][0]["conversation_id"]
        source = next(row for row in normalized if row["conversation_id"] == key)
        source["messages"][-1]["content"] += text
        self.rewrite("normalized.jsonl", normalized, jsonl=True)
        final = read_jsonl(self.data / "final.jsonl")
        final[0]["messages"] = source["messages"]
        self.rewrite("final.jsonl", final, jsonl=True)

    def test_numeric_order_alias_and_item_only_references_are_excluded(self):
        self.inject_references(" 関連注文ORD-001327、関連商品ITEM-001336-1。")
        bundle = self.bundle()
        choices = {c["category"]: c for c in bundle["provenance"]["selection"]}
        self.assertEqual(choices["shipping"]["selected_order_number"], 2647)
        self.assertEqual(choices["refund_gold"]["selected_order_number"], 2656)
        self.assertIn("final.jsonl", choices["shipping"]["excluded_candidates"][0]["sources"])

    def test_exhausted_scenario_fails_without_dropping_cases(self):
        self.inject_references(" ".join(f"ORD-{1327 + PERIOD * n:04d}"
                                      for n in range(CANDIDATES_PER_SCENARIO)))
        with self.assertRaisesRegex(ValueError, "exhausted for shipping"):
            self.bundle()
        self.assertFalse(self.output.exists())

    def test_identical_output_reused_without_writes(self):
        first = self.bundle()
        second = prepare_cases(self.data, self.directory / "second.json")
        self.assertEqual(first, second)
        # Formatting differences are not experiment changes.
        self.output.write_text(canonical(first), encoding="utf-8")
        content = self.output.read_bytes()
        modified = self.output.stat().st_mtime_ns
        with patch("foundry_distillation_lab.evaluation.cases.write_json",
                   side_effect=AssertionError("existing input must not be written")):
            self.assertEqual(first, self.bundle())
        self.assertEqual(content, self.output.read_bytes())
        self.assertEqual(modified, self.output.stat().st_mtime_ns)
        self.assertEqual(first, read_json(self.output))

    def test_differing_or_corrupt_existing_output_requires_new_path(self):
        bundle = self.bundle()
        bundle["cases"][0]["user_input"] += " changed"
        self.output.write_text(canonical(bundle), encoding="utf-8")
        for content in (self.output.read_bytes(), b"{invalid"):
            self.output.write_bytes(content)
            with self.assertRaisesRegex(FileExistsError, "choose a new output path"):
                self.bundle()
            self.assertEqual(content, self.output.read_bytes())

    def test_existing_output_does_not_bypass_source_validation(self):
        self.bundle()
        before = self.output.read_bytes()
        (self.data / "final.jsonl").unlink()
        with self.assertRaises(FileNotFoundError):
            self.bundle()
        self.assertEqual(before, self.output.read_bytes())

    def test_missing_files_hash_mismatch_and_held_rows_fail_closed(self):
        path = self.data / "final.jsonl"
        content = path.read_bytes()
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.bundle()
        path.write_bytes(content + b"\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.bundle()
        path.write_bytes(content)
        audit = read_json(self.data / "audit.json")
        audit["rows"][0]["status"] = "held"
        self.rewrite("audit.json", audit)
        with self.assertRaisesRegex(ValueError, "skipped/held/invalid"):
            self.bundle()
        self.assertFalse(self.output.exists())

    def test_valid_hash_does_not_hide_corrupt_or_inconsistent_data(self):
        rows = read_jsonl(self.data / "train.jsonl")
        rows[0]["messages"][-1]["content"] += " altered"
        self.rewrite("train.jsonl", rows, jsonl=True)
        with self.assertRaisesRegex(ValueError, "differs from normalized"):
            self.bundle()
        self.assertFalse(self.output.exists())

    def test_malformed_json_and_empty_data_are_rejected(self):
        path = self.data / "normalized.jsonl"
        for raw in ('{"bad":\n', ""):
            path.write_text(raw, encoding="utf-8")
            manifest = read_json(self.data / "manifest.json")
            manifest["output_hashes"][path.name] = sha256(path)
            self.rewrite("manifest.json", manifest)
            with self.assertRaises(ValueError):
                self.bundle()
        self.assertFalse(self.output.exists())

    def test_cli_offline_success_and_missing_prerequisite_error(self):
        command = [sys.executable, str(ROOT / "scripts" / "prepare_evaluation.py"),
                   "--data-dir", str(self.data), "--output", str(self.output)]
        result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("11 supplemental development cases", result.stdout)
        result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        altered = read_json(self.output)
        altered["records"] = [{"unexpected": "executed input"}]
        self.output.write_text(canonical(altered), encoding="utf-8")
        result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("choose a new output path", result.stderr)
        command[-3] = str(self.directory / "missing")
        command[-1] = str(self.directory / "never.json")
        result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("preparation stopped", result.stderr)


if __name__ == "__main__":
    unittest.main()
