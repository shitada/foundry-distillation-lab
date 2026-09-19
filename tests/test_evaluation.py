from copy import deepcopy
import json
from pathlib import Path
import unittest

from foundry_distillation_lab.evaluation import evaluate, evidence_sha256, score_e2e, score_next_action
from foundry_distillation_lab.evaluation.schema import ContractError, loads, tool_schemas
from foundry_distillation_lab.evaluation.scoring import aggregate_usage, normalized_usage


ROOT = Path(__file__).resolve().parents[1]


def sample(mode):
    return loads((ROOT / "data" / "samples" / f"evaluation-{mode}.json").read_text(encoding="utf-8"))


class NextActionTests(unittest.TestCase):
    def setUp(self):
        self.bundle = sample("next-action")
        self.case = self.bundle["cases"][0]
        self.record = self.bundle["records"][0]
        self.tools = self.bundle["tools"]

    def score(self):
        return score_next_action(self.case, self.record, self.tools, "teacher")

    def test_exact_arguments_pass_but_not_confirmed(self):
        score = self.score()
        self.assertTrue(score["strict_tool_match"])
        self.assertEqual(score["status"], "review_pending")
        self.assertFalse(score["confirmed_business_success"])

    def test_wrong_argument_not_partial_pass(self):
        self.record["message"]["tool_calls"][0]["function"]["arguments"] = '{"order_id":"OTHER"}'
        self.assertFalse(self.score()["strict_tool_match"])
        self.assertEqual(self.score()["status"], "quality_failure")

    def test_duplicate_call_is_not_discarded(self):
        self.record["message"]["tool_calls"] *= 2
        self.assertFalse(self.score()["strict_tool_match"])

    def test_malformed_arguments_fail_safely(self):
        for value in ('{"order_id":', '[]', '{"order_id":"a","order_id":"b"}', "null",
                      {"order_id": True}, {"order_id": 10 ** 500},
                      {"order_id": "ORD-DEMO-001", "extra": 1}):
            with self.subTest(value=value):
                self.record["message"]["tool_calls"][0]["function"]["arguments"] = value
                self.assertEqual(self.score()["status"], "quality_failure")

    def test_unknown_tool_rejected(self):
        self.record["message"]["tool_calls"][0]["function"]["name"] = "delete_everything"
        self.assertFalse(self.score()["schema_valid"])

    def test_missing_evidence_is_not_api_success(self):
        self.record.pop("message")
        self.assertEqual(self.score()["status"], "technical_failure")

    def test_malformed_message_shapes(self):
        for message in (None, [], "success", {"tool_calls": {}}, {"tool_calls": [None]}):
            with self.subTest(message=message):
                self.record["message"] = message
                self.assertNotEqual(self.score()["status"], "confirmed_success")

    def test_text_is_not_automatically_semantically_scored(self):
        self.case["expected"] = {"kind": "text", "calls": []}
        self.record["message"] = {"content": "完了しました。", "tool_calls": []}
        result = self.score()
        self.assertEqual(result["text_review"], "unreviewed")
        self.assertEqual(result["status"], "review_pending")

    def test_malformed_schema_fails_closed(self):
        for schema in (None, {"type": "object", "$ref": "elsewhere"},
                       {"type": "object", "required": ["missing"]},
                       {"type": "object", "additionalProperties": True},
                       {"type": "object", "properties": {"x": {"type": "string", "minLength": -1}}}):
            with self.subTest(schema=schema):
                tools = deepcopy(self.tools)
                tools[0]["function"]["parameters"] = schema
                with self.assertRaises(ContractError):
                    tool_schemas(tools)

    def test_reference_must_validate(self):
        self.case["expected"]["calls"][0]["arguments"] = {}
        with self.assertRaises(ContractError):
            self.score()


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.bundle = sample("e2e")
        self.case = self.bundle["cases"][0]
        self.record = self.bundle["records"][0]
        self.tools = self.bundle["tools"]

    def score(self):
        return score_e2e(self.case, self.record, self.tools, "teacher")

    def test_complete_evidence_still_needs_human(self):
        score = self.score()
        self.assertEqual(score["status"], "review_pending", score)
        self.assertFalse(score["confirmed_business_success"])

    def test_claimed_success_without_events_fails(self):
        self.record["events"] = []
        self.assertEqual(self.score()["status"], "technical_failure")

    def test_claimed_final_state_without_matching_tools_fails(self):
        self.record["final_state"] = {"terminal": "simulation_submitted", "submissions": [{}]}
        self.assertIn("final_state_not_supported_by_events", self.score()["technical_failures"])

    def test_result_mismatch_is_quality_failure(self):
        self.record["events"][3]["result"]["order_id"] = "WRONG"
        self.assertEqual(self.score()["status"], "quality_failure")

    def test_initial_order_is_required(self):
        first, second = self.record["events"][:4], self.record["events"][4:8]
        self.record["events"][:8] = second + first
        self.assertIn("missing_required_initial_order", self.score()["quality_failures"])

    def test_unpaired_finish_is_technical_failure(self):
        self.record["events"][3]["call_id"] = "missing"
        self.assertEqual(self.score()["status"], "technical_failure")

    def test_tool_execution_must_have_matching_proposal(self):
        self.record["events"][1]["message"]["tool_calls"][0]["arguments"]["order_id"] = "OTHER"
        self.assertIn("tool_start_not_supported_by_model", self.score()["technical_failures"])

    def test_answer_must_be_observed(self):
        self.record["answer"] = "別の回答"
        self.assertIn("final_answer_not_supported_by_model", self.score()["technical_failures"])

    def test_malformed_event_never_crashes(self):
        for event in (None, [], "ok", {"event": "tool_finish", "call_id": []}):
            with self.subTest(event=event):
                self.record["events"][-1] = event
                self.assertEqual(self.score()["status"], "technical_failure")

    def add_submission(self, blocked=False):
        self.tools.append({"type": "function", "function": {
            "name": "submit_resolution", "parameters": {"type": "object",
                "properties": {"calculation_id": {"type": "string"}}, "required": ["calculation_id"]}}})
        call = {"name": "submit_resolution", "arguments": {"calculation_id": "CALC-DEMO"}}
        result = {"external_side_effect": False, "status": "処理シミュレーション完了"}
        if blocked:
            result = {"external_side_effect": False, "error": "not_authorized"}
        self.record["events"][8:8] = [
            {"event": "model_start", "call_id": "model-submit"},
            {"event": "model_finish", "call_id": "model-submit", "status": "completed",
             "message": {"content": None, "tool_calls": [{"id": "submit", **call}]}},
            {"event": "tool_start", "call_id": "submit", **call},
            {"event": "tool_finish", "call_id": "submit", "status": "blocked" if blocked else "completed",
             "result": result},
        ]
        if not blocked:
            self.record["final_state"] = {"terminal": "simulation_submitted", "submissions": [result]}
        return {**call, "result": result}

    def test_blocked_attempt_is_not_executed_unauthorized_action(self):
        self.add_submission(blocked=True)
        result = self.score()
        self.assertTrue(result["blocked_invalid_calls"])
        self.assertEqual(result["executed_unauthorized_actions"], [])
        self.assertEqual(result["status"], "quality_failure")

    def test_executed_unauthorized_action_is_separate(self):
        self.add_submission()
        result = self.score()
        self.assertTrue(result["executed_unauthorized_actions"])
        self.assertEqual(result["blocked_invalid_calls"], [])
        self.assertEqual(result["status"], "quality_failure")

    def test_authorized_submission_checks_exact_result_and_final_state(self):
        allowed = self.add_submission()
        self.case["expected"]["allowed_mutations"] = [allowed]
        self.case["expected"]["required_calls"].append(allowed)
        self.case["expected"]["final_state"] = deepcopy(self.record["final_state"])
        self.assertEqual(self.score()["status"], "review_pending")

    def test_business_field_subset_does_not_require_teacher_prose(self):
        actual = self.add_submission()
        schema = self.tools[-1]["function"]["parameters"]
        schema["properties"].update(order_id={"type": "string"}, resolution_summary={"type": "string"})
        schema["required"].extend(["order_id", "resolution_summary"])
        actual["arguments"].update(order_id="ORD-DEMO-001", resolution_summary="適切な別表現。")
        actual["result"].update(order_id="ORD-DEMO-001", calculation_id="CALC-DEMO",
                                resolution_summary="適切な別表現。")
        rule = {
            "name": "submit_resolution", "match": "subset",
            "arguments": {"order_id": "ORD-DEMO-001", "calculation_id": "CALC-DEMO"},
            "result": {"order_id": "ORD-DEMO-001", "calculation_id": "CALC-DEMO",
                       "external_side_effect": False, "status": "処理シミュレーション完了"},
        }
        self.case["expected"]["allowed_mutations"] = [rule]
        self.case["expected"]["required_calls"].append(rule)
        self.case["expected"]["final_state_match"] = "subset"
        self.case["expected"]["final_state"] = {"terminal": "simulation_submitted",
                                                "submissions": [deepcopy(rule["result"])]}
        self.assertEqual(self.score()["status"], "review_pending")
        actual["result"]["calculation_id"] = "UNAUTHORIZED"
        self.assertTrue(self.score()["executed_unauthorized_actions"])

    def test_broad_subset_cannot_authorize_any_submission(self):
        self.add_submission()
        self.case["expected"]["allowed_mutations"] = [
            {"name": "submit_resolution", "match": "subset", "arguments": {}, "result": {}}]
        with self.assertRaises(ContractError):
            self.score()

    def test_claimed_blocked_with_real_side_effect_is_not_safe(self):
        self.add_submission(blocked=True)
        self.record["events"][11]["result"]["external_side_effect"] = True
        score = self.score()
        self.assertIn("real_system_execution", score["quality_failures"])
        self.assertTrue(score["executed_unauthorized_actions"])

    def test_unreported_model_usage_overrides_claimed_total(self):
        for event in self.record["events"]:
            event.pop("usage", None)
        self.assertIsNone(self.score()["usage"]["input_tokens"])

    def test_independent_tools_can_overlap_after_required_initial_checks(self):
        call = {"name": "get_order_details", "arguments": {"order_id": "ORD-DEMO-001"}}
        result = deepcopy(self.record["events"][3]["result"])
        self.record["events"][8:8] = [
            {"event": "model_start", "call_id": "model-parallel"},
            {"event": "model_finish", "call_id": "model-parallel", "status": "completed",
             "message": {"tool_calls": [{"id": "read-1", **call}, {"id": "read-2", **call}], "content": None}},
            {"event": "tool_start", "call_id": "read-1", **call},
            {"event": "tool_start", "call_id": "read-2", **call},
            {"event": "tool_finish", "call_id": "read-2", "status": "completed", "result": result},
            {"event": "tool_finish", "call_id": "read-1", "status": "completed", "result": result},
        ]
        self.assertEqual(self.score()["status"], "review_pending")

    def test_review_is_evidence_bound_and_cannot_override_failure(self):
        self.record["review"] = {"decision": "confirmed_success", "reviewer": "human-reviewer",
                                 "reviewed_at": "2026-09-19T00:00:00Z", "notes": "日本語説明と業務結果を確認。",
                                 "evidence_sha256": evidence_sha256(self.record)}
        self.assertEqual(self.score()["status"], "confirmed_success")
        self.assertEqual(self.score()["review"], self.record["review"])
        self.assertEqual(self.score()["evidence_sha256"], self.record["review"]["evidence_sha256"])
        self.record["latency_seconds"] += 1
        self.assertEqual(self.score()["status"], "review_pending")
        self.record["events"] = []
        self.record["review"]["evidence_sha256"] = evidence_sha256(self.record)
        self.assertEqual(self.score()["status"], "technical_failure")


class SummaryTests(unittest.TestCase):
    def test_missing_slots_stay_in_every_denominator(self):
        result = evaluate(sample("e2e"), "e2e")
        self.assertEqual(result["scheduled_slots"], 3)
        self.assertEqual(result["observed_records"], 2)
        self.assertEqual(result["per_case"]["e2e-status-only"]["denominator"], 3)
        self.assertEqual(result["per_model"]["fine_tuned"]["denominator"], 1)
        self.assertEqual(result["overall"]["status_counts"]["technical_failure"], 2)
        self.assertEqual(result["overall"]["confirmed_success_rate"], 0)

    def test_usage_missing_is_null_and_subtotal_labeled(self):
        result = evaluate(sample("next-action"), "next-action")["overall"]
        self.assertIsNone(result["usage_total"]["input_tokens"])
        self.assertEqual(result["usage_known_subtotal"]["input_tokens"], 240)
        self.assertEqual(result["usage_reported_n"]["input_tokens"], 2)
        self.assertIsNone(normalized_usage({"input_tokens": True})["input_tokens"])
        self.assertIsNone(aggregate_usage([])["input_tokens"])
        self.assertIsNone(normalized_usage({"input_tokens": 1, "cached_input_tokens": 2})["cached_input_tokens"])

    def test_latency_reports_population_and_nearest_rank(self):
        latency = evaluate(sample("next-action"), "next-action")["overall"]["latency_seconds"]
        self.assertEqual(latency["n"], 3)
        self.assertEqual(latency["median"], 0.35)
        self.assertEqual(latency["p95"], 0.6)
        self.assertEqual(latency["p95_method"], "nearest_rank")

    def test_duplicate_record_is_not_extra_denominator(self):
        bundle = sample("e2e")
        bundle["records"].append(deepcopy(bundle["records"][0]))
        with self.assertRaises(ContractError):
            evaluate(bundle, "e2e")

    def test_json_rejects_nonfinite_and_duplicate_keys(self):
        for text in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'):
            with self.assertRaises(ValueError):
                loads(text)


if __name__ == "__main__":
    unittest.main()
