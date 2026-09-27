"""Model mistakes terminate one case; missing execution evidence still stops a run."""

from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock

from foundry_distillation_lab.evaluation.grading import grade_run
from foundry_distillation_lab.evaluation.runner import run_case
from foundry_distillation_lab.evaluation.scoring import evidence_sha256, score_e2e
from foundry_distillation_lab.evaluation.workflow import compare_runs, run_evaluation
from foundry_distillation_lab.io import read_json, write_json
from foundry_distillation_lab.retail import RetailSession


USAGE = {"input_tokens": 13, "output_tokens": 5, "cached_input_tokens": 0}


def call(name="get_order_details", arguments=None, identifier="lookup"):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(
            {"order_id": "ORD-0001"} if arguments is None else arguments)}}


def reply(calls=None, content=None):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {"message": message, "usage": deepcopy(USAGE)}


def case():
    return {"case_id": "one", "user_input": "注文ORD-0001の配送状況を教えてください。",
            "expected": {"initial_tools": ["get_order_details", "get_fulfillment_status"],
                         "required_calls": [], "allowed_mutations": [],
                         "final_state": {"terminal": "answer_only", "submissions": []}}}


class CaseTerminationTests(unittest.TestCase):
    def score(self, record):
        return score_e2e(case(), record, RetailSession().tools, "base")

    def assert_quality_failure(self, record, reason):
        score = self.score(record)
        self.assertEqual(score["technical_failures"], [], score)
        self.assertEqual(score["status"], "quality_failure")
        self.assertEqual(score["automatic_decision"], "failure")
        self.assertIn(reason, score["quality_failures"])
        self.assertFalse(score["confirmed_business_success"])
        self.assertEqual(score["usage"], record["usage"])

    def test_invalid_names_arguments_and_identifiers_are_assessed_failures(self):
        invalid_json = call()
        invalid_json["function"]["arguments"] = "{invalid"
        missing_id = call()
        del missing_id["id"]
        for proposals in ([call("not_a_tool")], [call(arguments={})],
                          [call(arguments={"order_id": 1})], [invalid_json],
                          [missing_id], [call(), call()], ["not an object"]):
            with self.subTest(proposals=proposals):
                session = RetailSession()
                session.call = Mock(side_effect=AssertionError("invalid batch must not execute"))
                invoke = Mock(return_value=reply(proposals))
                record = run_case(case(), "base", invoke, lambda: session)
                self.assert_quality_failure(record, "invalid_tool_call")
                self.assertEqual(record["status"], "blocked")
                self.assertEqual(invoke.call_count, 1)
                session.call.assert_not_called()
                blocks = [event for event in record["events"] if event["event"] == "tool_blocked"]
                self.assertEqual([event["proposal"] for event in blocks], proposals)

    def test_mixed_batch_accounts_for_valid_but_unexecuted_proposal(self):
        proposals = [call(), call("not_a_tool", identifier="invalid")]
        record = run_case(case(), "base", Mock(return_value=reply(proposals)), RetailSession)
        self.assert_quality_failure(record, "invalid_tool_call")
        score = self.score(record)
        self.assertEqual(len(score["blocked_invalid_calls"]), 1)
        self.assertEqual(len(score["unexecuted_calls"]), 2)
        self.assertEqual(record["tool_calls"], 0)

    def test_repeated_id_after_a_completed_tool_does_not_reexecute(self):
        session = RetailSession()
        original = session.call
        session.call = Mock(side_effect=original)
        invoke = Mock(return_value=reply([call()]))
        record = run_case(case(), "base", invoke, lambda: session)
        self.assert_quality_failure(record, "invalid_tool_call")
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(session.call.call_count, 1)
        self.assertEqual(record["usage"]["input_tokens"], 26)

    def test_model_and_tool_limits_end_only_the_case(self):
        record = run_case(case(), "base", Mock(return_value=reply([call()])),
                          RetailSession, max_model_calls=1)
        self.assert_quality_failure(record, "model_call_limit")
        self.assertEqual(record["model_calls"], 1)
        proposals = [call(), call("get_fulfillment_status", identifier="delivery")]
        record = run_case(case(), "base", Mock(return_value=reply(proposals)),
                          RetailSession, max_tool_calls=1)
        self.assert_quality_failure(record, "tool_call_limit")
        self.assertEqual(record["tool_calls"], 0)
        self.assertEqual(len(self.score(record)["unexecuted_calls"]), 2)

    def test_safe_business_rejection_accounts_for_remaining_batch(self):
        session = RetailSession()
        session.call = Mock(return_value={"error": "business_rule", "external_side_effect": False})
        proposals = [call(), call("get_fulfillment_status", identifier="delivery")]
        record = run_case(case(), "base", Mock(return_value=reply(proposals)), lambda: session)
        self.assert_quality_failure(record, "tool_rejected")
        self.assertEqual(session.call.call_count, 1)
        self.assertEqual(len(self.score(record)["unexecuted_calls"]), 1)

    def test_generic_tool_exceptions_and_unproven_side_effects_remain_unknown(self):
        for error in (ValueError("unclassified domain or programming error"), RuntimeError("failure")):
            session = RetailSession()
            session.call = Mock(side_effect=error)
            record = run_case(case(), "base", Mock(return_value=reply([call()])), lambda: session)
            self.assertEqual(self.score(record)["status"], "technical_failure")
            self.assertNotIn("termination", record)
        session.call = Mock(return_value={"error": "unknown_side_effect"})
        record = run_case(case(), "base", Mock(return_value=reply([call()])), lambda: session)
        self.assertEqual(self.score(record)["automatic_decision"], "unknown")

    def test_empty_model_answer_is_a_quality_failure(self):
        for content in ("", "   ", None):
            record = run_case(case(), "base", Mock(return_value=reply(content=content)), RetailSession)
            self.assert_quality_failure(record, "empty_answer")

    def test_valid_but_wrong_tool_is_executed_and_scored_without_guidance(self):
        session = RetailSession()
        original = session.call
        session.call = Mock(side_effect=original)
        invoke = Mock(side_effect=[
            reply([call("get_fulfillment_status", identifier="delivery")]),
            reply(content="配達済みです。"),
        ])
        record = run_case(case(), "base", invoke, lambda: session)
        self.assertEqual(record["status"], "completed")
        self.assertNotIn("termination", record)
        self.assert_quality_failure(record, "missing_required_initial_order")
        session.call.assert_called_once_with("get_fulfillment_status", {"order_id": "ORD-0001"})
        second_input = invoke.call_args_list[1].args[0]
        self.assertEqual(second_input["messages"][-1]["role"], "tool")
        self.assertNotIn("expected", second_input)

    def test_missing_or_changed_witness_does_not_hide_technical_failure(self):
        original = run_case(case(), "base", Mock(return_value=reply([call(), call("unknown", identifier="bad")])),
                            RetailSession)
        variants = []
        changed = deepcopy(original)
        del changed["termination"]
        variants.append(changed)
        changed = deepcopy(original)
        del changed["events"][1]
        variants.append(changed)
        changed = deepcopy(original)
        del changed["events"][-2]
        variants.append(changed)
        changed = deepcopy(original)
        changed["events"][-2]["proposal"] = call()
        variants.append(changed)
        changed = deepcopy(original)
        changed["termination"]["reason"] = "tool_call_limit"
        changed["events"][-1]["termination"]["reason"] = "tool_call_limit"
        variants.append(changed)
        changed = deepcopy(original)
        changed["final_state"] = {"terminal": "simulation_submitted", "submissions": [{}]}
        variants.append(changed)
        for record in variants:
            with self.subTest(record=record):
                self.assertEqual(self.score(record)["automatic_decision"], "unknown")
                self.assertTrue(self.score(record)["technical_failures"])

    def test_a_human_success_cannot_override_model_failure(self):
        record = run_case(case(), "base", Mock(return_value=reply([call("unknown")])), RetailSession)
        record["review"] = {"decision": "confirmed_success", "reviewer": "fixture",
                           "reviewed_at": "2026-09-27T00:00:00Z", "notes": "Fixture only",
                           "evidence_sha256": evidence_sha256(record)}
        self.assert_quality_failure(record, "invalid_tool_call")


class WholeRunContinuationTests(unittest.TestCase):
    def test_eleven_cases_grade_and_compare_after_first_model_mistake(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = RetailSession()
            cases = []
            for number in range(1, 12):
                item = deepcopy(case())
                item.update(case_id=str(number), user_input=f"注文ORD-{number:04d}の配送状況を教えてください。",
                            system_prompt=session.system_prompt)
                item["expected"]["required_calls"] = [
                    {"name": name, "arguments": {"order_id": f"ORD-{number:04d}"}}
                    for name in ("get_order_details", "get_fulfillment_status")]
                cases.append(item)
            write_json(root / "input.json", {
                "schema": "retail-evaluation-input-v1", "models": ["base", "fine_tuned"],
                "records": [], "tools": session.tools, "cases": cases})
            write_json(root / "config.json", {
                "base_url": "https://example.invalid/openai/v1/",
                "targets": {"base": "base", "fine_tuned": "fine"},
                "max_completion_tokens": 1024, "timeout_seconds": 60,
                "max_model_calls": 12, "max_tool_calls": 24})
            write_json(root / "judge.json", {
                "base_url": "https://example.invalid/openai/v1/", "deployment": "grader",
                "max_completion_tokens": 1024, "timeout_seconds": 60})
            requests = []

            def respond(payload):
                requests.append(deepcopy(payload))
                number = re.search(r"ORD-(\d+)", payload["messages"][1]["content"]).group(1)
                if payload["model_label"] == "base" and number == "0001":
                    return reply([call("unknown")])
                results = sum(message["role"] == "tool" for message in payload["messages"])
                if results < 2:
                    name = ("get_order_details", "get_fulfillment_status")[results]
                    return reply([call(name, {"order_id": f"ORD-{number}"}, f"step-{results}")])
                return reply(content="配送状況を確認しました。")

            judge = Mock(return_value={
                "finish_reason": "stop", "response_model": "fixture-grader",
                "message": {"content": '{"decision":"success","reason":"架空の採点です。"}'},
                "usage": USAGE})
            reports = {}
            for model in ("base", "fine_tuned"):
                report, complete = run_evaluation(
                    root / "input.json", root / "config.json", "e2e", model, root / model, invoke=respond)
                self.assertTrue(complete)
                self.assertEqual(report["observed_records"], 11)
                self.assertEqual(report["scheduled_slots"], 11)
                self.assertEqual(report["overall"]["status_counts"]["technical_failure"], 0)
                reports[model], complete = grade_run(
                    root / model, root / "judge.json", root / f"{model}-graded", invoke=judge)
                self.assertTrue(complete)
            self.assertEqual(reports["base"]["overall"]["automatic_decision_counts"],
                             {"success": 10, "failure": 1, "needs_review": 0, "unknown": 0})
            self.assertEqual(judge.call_count, 21)
            base_requests = [request for request in requests if request["model_label"] == "base"]
            self.assertEqual(len(base_requests), 31)
            self.assertEqual(reports["base"]["overall"]["usage_total"]["input_tokens"], 31 * 13)
            comparison = compare_runs(root / "base-graded", root / "fine_tuned-graded", root / "comparison")
            self.assertEqual(comparison["decision_counts"],
                             {"improvement": 1, "regression": 0, "no_change": 10, "incomparable": 0})
            self.assertEqual(read_json(root / "base" / "evidence.json")["records"][0]["status"], "blocked")

            for exception in (TimeoutError("network"), PermissionError("authentication")):
                invoke = Mock(side_effect=exception)
                path = root / type(exception).__name__
                report, complete = run_evaluation(
                    root / "input.json", root / "config.json", "e2e", "base", path, invoke=invoke)
                self.assertFalse(complete)
                self.assertEqual(report["observed_records"], 1)
                self.assertEqual(report["scheduled_slots"], 11)
                self.assertEqual(invoke.call_count, 1)


if __name__ == "__main__":
    unittest.main()
