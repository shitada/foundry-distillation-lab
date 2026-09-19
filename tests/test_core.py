from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import tempfile
import unittest

from foundry_distillation_lab.datasets.prepare import normalize, partitions, prepare
from foundry_distillation_lab.io import read_json, sha256, write_json, write_jsonl
from foundry_distillation_lab.retail import RetailSession
from foundry_distillation_lab.safety import Approval, Journal

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("generate_samples", ROOT / "scripts" / "generate_samples.py")
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class RetailTests(unittest.TestCase):
    def test_japanese_contract_and_fixed_date(self):
        session = RetailSession()
        self.assertEqual(len(session.tools), 6)
        result = session.call("get_order_details", {"order_id": "ORD-0001"})
        self.assertEqual(result["currency"], "JPY")
        self.assertEqual(result["order_date"], "2026-08-10")
        self.assertEqual(result["items"][0]["name"], "ランニングシューズ")
        self.assertEqual(result["items"][0]["unit_price_jpy"], 9100)

    def test_lost_refund_and_state_isolation(self):
        first, second = RetailSession(), RetailSession()
        args = {"order_id": "ORD-0003", "item_id": "ITEM-0003-2", "reason": "未着"}
        policy = first.call("check_resolution_policy", args)
        self.assertEqual(policy["eligible_actions"], ["代替品発送", "返金"])
        requested = {"order_id": "ORD-0003", "items": [{
            "item_id": "ITEM-0003-2", "reason": "未着", "actions": ["返金"],
            "policy_id": policy["policy_id"]}]}
        with self.assertRaisesRegex(ValueError, "未発行"):
            second.call("calculate_resolution", requested)
        calculation = first.call("calculate_resolution", requested)
        self.assertEqual(calculation["total_refund_jpy"], 30000)
        self.assertEqual(calculation["total_restocking_fee_jpy"], 0)
        with self.assertRaisesRegex(ValueError, "calculation_id"):
            second.call("submit_resolution", {"order_id": "ORD-0003",
                        "calculation_id": calculation["calculation_id"], "resolution_summary": "返金"})

    def test_schema_rejects_missing_argument(self):
        with self.assertRaisesRegex(ValueError, "Missing"):
            RetailSession().call("calculate_resolution", {"order_id": "ORD-0001",
                "items": [{"item_id": "ITEM-0001-1", "reason": "破損", "actions": ["返金"]}]})

    def test_late_credit_is_independent_expected_value(self):
        session = RetailSession()
        policy = session.call("check_resolution_policy", {
            "order_id": "ORD-0004", "item_id": "ITEM-0004-1", "reason": "破損"})
        result = session.call("calculate_resolution", {"order_id": "ORD-0004", "items": [{
            "item_id": "ITEM-0004-1", "reason": "破損",
            "actions": ["返金", "配送遅延クレジット"], "policy_id": policy["policy_id"]}]})
        self.assertEqual(result["total_credit_jpy"], 1000)
        self.assertEqual(result["total_refund_jpy"], 10000)


class DatasetTests(unittest.TestCase):
    def test_short_and_multi_customer_trace_rejected(self):
        row = next(generator.samples())
        short = dict(row, messages=[{"role": "system", "content": RetailSession().system_prompt}])
        with self.assertRaisesRegex(ValueError, "incomplete"):
            normalize(short)
        row["messages"].extend([{"role": "user", "content": "追加で教えてください。"},
                                {"role": "assistant", "content": "確認します。"}])
        with self.assertRaisesRegex(ValueError, "multiple_customer"):
            normalize(row)

    def test_shared_order_never_crosses_partitions(self):
        rows = [normalize(row)[0] for row in generator.samples()]
        shared = deepcopy(rows[0])
        shared["conversation_id"] = "shared-order"
        shared["messages"][1]["content"] += "同じ注文の別の質問です。"
        rows.append(shared)
        split = partitions(rows)
        for values in split.values():
            ids = {r["conversation_id"] for r in values}
            self.assertEqual("shared-order" in ids, "sample-001" in ids)

    def test_full_offline_pipeline_and_immutable_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_jsonl(root / "input.jsonl", list(generator.samples()))
            manifest = prepare(root / "input.jsonl", root / "prepared")
            self.assertEqual([len(v) for v in manifest["partitions"].values()], [12, 3, 3, 2])
            self.assertFalse(manifest["quality_review_approved"])
            self.assertIn("illustrative_scripted_not_model_output", manifest["source_kinds"])
            with self.assertRaises(FileExistsError):
                prepare(root / "input.jsonl", root / "prepared")

    def test_duplicate_and_tool_sequence_rejected(self):
        row = next(generator.samples())
        row["messages"][2]["tool_call_id"] = "orphan"
        with self.assertRaisesRegex(ValueError, "orphan"):
            normalize(row)
        normalized = [normalize(r)[0] for r in generator.samples()]
        with self.assertRaisesRegex(ValueError, "duplicate_conversation"):
            partitions(normalized + [normalized[0]])
        duplicate = deepcopy(normalized[0])
        duplicate["conversation_id"] = "different-id"
        with self.assertRaisesRegex(ValueError, "duplicate_semantic"):
            partitions(normalized + [duplicate])

    def test_literal_null_removed_but_real_text_preserved(self):
        row = next(generator.samples())
        row["messages"][1]["content"] = "null"
        normalized, changes = normalize(row)
        self.assertNotIn("content", normalized["messages"][2])
        self.assertTrue(changes)
        row["messages"][1]["content"] = "注文を確認します。"
        self.assertEqual(normalize(row)[0]["messages"][2]["content"], "注文を確認します。")

    def test_private_content_held_with_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            row = next(generator.samples())
            row["messages"][0]["content"] += " a@example.com"
            write_jsonl(root / "input.jsonl", [row])
            with self.assertRaisesRegex(ValueError, "Rows held"):
                prepare(root / "input.jsonl", root / "prepared")
            audit = read_json(root / "prepared" / "audit.json")
            self.assertEqual(audit["rows"][0]["status"], "held")
            self.assertFalse((root / "prepared" / "train.jsonl").exists())


class SafetyTests(unittest.TestCase):
    def approval(self, root, **overrides):
        write_json(root / "payload.json", {"example": True})
        data = {"approved": True, "operation": "test", "target": "test-model",
                "input_sha256": sha256(root / "payload.json"), "currency": "USD",
                "max_requests": 2, "max_cost": 1,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        data.update(overrides)
        write_json(root / "approval.json", data)
        return Approval.load(root / "approval.json", "test", root / "payload.json")

    def test_durable_budget_and_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approval = self.approval(root)
            approval.assert_target("test-model")
            with self.assertRaises(ValueError):
                approval.assert_target("other")
            approval.reserve(.6)
            reloaded = Approval.load(root / "approval.json", "test", root / "payload.json")
            with self.assertRaisesRegex(ValueError, "limit"):
                reloaded.reserve(.5)
            reloaded.reserve(.4)
            with self.assertRaisesRegex(ValueError, "limit"):
                reloaded.reserve(0)

    def test_invalid_approval_rejected(self):
        for override in ({"approved": "true"}, {"max_requests": True},
                         {"expires_at": "2000-01-01T00:00:00Z"}, {"max_cost": -1},
                         {"input_sha256": "wrong"}, {"currency": ""}):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(ValueError):
                    self.approval(Path(temporary), **override)

    def test_attempt_cannot_be_replayed_or_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = Journal(temporary)
            journal.start("attempt-1", {"request": 1})
            with self.assertRaises(FileExistsError):
                Journal(temporary).start("attempt-1", {"request": 1})
            journal.finish("attempt-1", "unknown", {"usage": None})
            with self.assertRaises(FileExistsError):
                journal.finish("attempt-1", "success", {})
            with self.assertRaises(ValueError):
                journal.start("../escape", {})

    def test_changed_approval_and_abandoned_lock_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approval = self.approval(root)
            state = root / ".approval-state"
            state.mkdir()
            lock = state / (sha256(root / "approval.json") + ".lock")
            lock.write_text("abandoned", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                approval.reserve(.1)
            self.assertEqual(lock.read_text(encoding="utf-8"), "abandoned")
            with (root / "approval.json").open("a", encoding="utf-8") as stream:
                stream.write("\n")
            with self.assertRaisesRegex(ValueError, "changed"):
                approval.reserve(.1)


if __name__ == "__main__":
    unittest.main()
