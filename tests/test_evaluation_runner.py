from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from foundry_distillation_lab.evaluation import GuardedModel, OpenAITransport, run_case, run_next_action, score_e2e
from foundry_distillation_lab.evaluation.schema import ContractError, loads


ROOT = Path(__file__).resolve().parents[1]


def fixture():
    return loads((ROOT / "data" / "samples" / "evaluation-e2e.json").read_text(encoding="utf-8"))


class FakeSession:
    def __init__(self):
        self.tools = fixture()["tools"]
        self.system_prompt = "架空の注文を確認してください。"
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        expected = fixture()["cases"][0]["expected"]["required_calls"]
        return deepcopy(next(entry["result"] for entry in expected if entry["name"] == name))


def model_responses():
    return [{"message": deepcopy(event["message"]), "usage": event.get("usage")}
            for event in fixture()["records"][0]["events"] if event["event"] == "model_finish"]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture()["cases"][0]

    def test_isolated_session_for_each_case_and_model(self):
        sessions = []
        def factory():
            session = FakeSession()
            sessions.append(session)
            return session
        for model in ("teacher", "base", "fine_tuned"):
            responses = iter(model_responses())
            record = run_case(self.case, model, lambda payload: next(responses), factory)
            result = score_e2e(self.case, record, fixture()["tools"], model)
            self.assertEqual(result["status"], "review_pending", result)
        self.assertEqual(len({id(session) for session in sessions}), 3)
        self.assertEqual([len(session.calls) for session in sessions], [2, 2, 2])

    def test_unknown_tool_is_blocked_before_any_execution(self):
        session = FakeSession()
        message = model_responses()[0]
        message["message"]["tool_calls"][0]["name"] = "unknown"
        invoke = Mock(return_value=message)
        record = run_case(self.case, "teacher", invoke, lambda: session)
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(session.calls, [])
        self.assertEqual(invoke.call_count, 1)

    def test_batch_validation_prevents_partial_execution(self):
        session = FakeSession()
        response = model_responses()[0]
        response["message"]["tool_calls"].append({"id": "bad", "name": "unknown", "arguments": {}})
        record = run_case(self.case, "teacher", lambda payload: response, lambda: session)
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(session.calls, [])

    def test_unknown_model_outcome_stops_without_retry(self):
        invoke = Mock(side_effect=TimeoutError)
        record = run_case(self.case, "teacher", invoke, FakeSession)
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(invoke.call_count, 1)
        self.assertIsNone(record["usage"]["input_tokens"])

    def test_unknown_second_request_does_not_disguise_usage_as_complete(self):
        invoke = Mock(side_effect=[model_responses()[0], TimeoutError()])
        record = run_case(self.case, "teacher", invoke, FakeSession)
        self.assertEqual(invoke.call_count, 2)
        self.assertIsNone(record["usage"]["input_tokens"])

    def test_tool_exception_is_unknown_not_safe_rejection(self):
        session = FakeSession()
        session.call = Mock(side_effect=RuntimeError("side effect may have happened"))
        invoke = Mock(return_value=model_responses()[0])
        record = run_case(self.case, "teacher", invoke, lambda: session)
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(session.call.call_count, 1)
        self.assertEqual(record["events"][-2]["status"], "unknown")

    def test_explicit_no_side_effect_rejection_is_blocked(self):
        session = FakeSession()
        session.call = Mock(return_value={"error": "not_authorized", "external_side_effect": False})
        record = run_case(self.case, "teacher", lambda payload: model_responses()[0], lambda: session)
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["tool_calls"], 1)

    def test_max_model_calls_bounded(self):
        invoke = Mock(return_value=model_responses()[0])
        record = run_case(self.case, "teacher", invoke, FakeSession, max_model_calls=1)
        self.assertEqual(record["status"], "limit_exceeded")
        self.assertEqual(invoke.call_count, 1)

    def test_max_tool_calls_bounded_before_batch(self):
        response = model_responses()[0]
        response["message"]["tool_calls"].extend(model_responses()[1]["message"]["tool_calls"])
        session = FakeSession()
        record = run_case(self.case, "teacher", lambda payload: response, lambda: session, max_tool_calls=1)
        self.assertEqual(record["status"], "limit_exceeded")
        self.assertEqual(session.calls, [])

    def test_repeated_call_id_is_not_replayed(self):
        invoke = Mock(return_value=model_responses()[0])
        record = run_case(self.case, "teacher", invoke, FakeSession)
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["tool_calls"], 1)
        self.assertEqual(invoke.call_count, 2)

    def test_oracle_is_not_sent_to_model(self):
        seen = []
        responses = iter(model_responses())
        def invoke(payload):
            seen.append(payload)
            return next(responses)
        run_case(self.case, "teacher", invoke, FakeSession)
        self.assertTrue(seen)
        for payload in seen:
            self.assertEqual(set(payload), {"attempt_id", "model_label", "messages", "tools"})
            self.assertNotIn("required_calls", json.dumps(payload))

    def test_contract_mismatch_stops_before_model(self):
        invoke = Mock()
        with self.assertRaises(ContractError):
            run_case(self.case, "teacher", invoke, FakeSession, tools=[])
        invoke.assert_not_called()

    def test_next_action_is_exactly_one_call(self):
        case = {"case_id": "next", "messages": [{"role": "user", "content": "注文を確認してください。"}]}
        invoke = Mock(return_value=model_responses()[0])
        record = run_next_action(case, "teacher", invoke, fixture()["tools"])
        self.assertEqual(record["status"], "completed")
        self.assertEqual(invoke.call_count, 1)

    def test_actual_retail_session_with_injected_model(self):
        from foundry_distillation_lab.retail import RetailSession
        case = deepcopy(self.case)
        case["user_input"] = "ORD-0001 の配送状況だけ教えてください。"
        for expected in case["expected"]["required_calls"]:
            expected["arguments"]["order_id"] = "ORD-0001"
            expected.pop("result")
        responses = model_responses()
        for response in responses[:-1]:
            response["message"]["tool_calls"][0]["arguments"]["order_id"] = "ORD-0001"
        responses[-1]["message"]["content"] = "ORD-0001 は配達済みです。"
        sessions = []
        def factory():
            session = RetailSession()
            sessions.append(session)
            return session
        for model in ("teacher", "base"):
            invoke = Mock(side_effect=deepcopy(responses))
            record = run_case(case, model, invoke, factory)
            self.assertEqual(record["status"], "completed")
            score = score_e2e(case, record, sessions[-1].tools, model)
            self.assertEqual(score["status"], "review_pending", score)
            self.assertEqual(record["events"][3]["result"]["order_id"], "ORD-0001")
            self.assertEqual(record["events"][7]["result"]["status"], "配達済み")
        self.assertIsNot(sessions[0]._store, sessions[1]._store)


class DiskJournal:
    """Test double with the required exclusive durable-start semantics."""
    def __init__(self, directory):
        self.directory = directory

    def start(self, attempt_id, payload):
        with (self.directory / f"{attempt_id}.start.json").open("x", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def finish(self, attempt_id, status, result):
        with (self.directory / f"{attempt_id}.finish.json").open("x", encoding="utf-8") as handle:
            json.dump({"status": status, "result": result}, handle)


class GuardAndCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / "runs" / f"evaluation-test-{uuid.uuid4().hex}"
        self.directory.mkdir(parents=True)
        self.approval = Mock(spec=["assert_target", "reserve"])
        self.journal = DiskJournal(self.directory)
        self.transport = Mock(return_value={"message": {"content": "確認しました。"}})
        self.payload = {"attempt_id": "attempt-1", "messages": [], "tools": []}

    def tearDown(self):
        for attempt in range(10):
            try:
                shutil.rmtree(self.directory)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1)

    def guarded(self, **kwargs):
        return GuardedModel(self.transport, approval=self.approval, journal=self.journal,
                            target="example-deployment", reserve_per_request=0.1, **kwargs)

    def test_send_must_be_explicit(self):
        with self.assertRaises(PermissionError):
            self.guarded()
        self.transport.assert_not_called()

    def test_target_budget_and_write_ahead_before_transport(self):
        def invoke(payload):
            self.assertTrue((self.directory / "attempt-1.start.json").exists())
            self.approval.assert_target.assert_called_once_with("example-deployment")
            self.approval.reserve.assert_called_once_with(0.1)
            return {"message": {"content": "確認済み"}}
        self.transport.side_effect = invoke
        self.guarded(send=True)(self.payload)
        self.assertTrue((self.directory / "attempt-1.finish.json").exists())

    def test_existing_unfinished_start_prohibits_resend(self):
        self.journal.start("attempt-1", self.payload)
        with self.assertRaises(FileExistsError):
            self.guarded(send=True)(self.payload)
        self.transport.assert_not_called()

    def test_completed_attempt_also_immutable(self):
        self.guarded(send=True)(self.payload)
        with self.assertRaises(FileExistsError):
            self.guarded(send=True)(self.payload)
        self.assertEqual(self.transport.call_count, 1)

    def test_transport_error_keeps_unknown_no_retry(self):
        self.transport.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            self.guarded(send=True)(self.payload)
        self.assertEqual(self.transport.call_count, 1)
        finish = json.loads((self.directory / "attempt-1.finish.json").read_text())
        self.assertEqual(finish["status"], "unknown")
        with self.assertRaises(FileExistsError):
            self.guarded(send=True)(self.payload)
        self.assertEqual(self.transport.call_count, 1)

    def test_reservation_failure_stops_before_network(self):
        self.approval.reserve.side_effect = PermissionError
        with self.assertRaises(PermissionError):
            self.guarded(send=True)(self.payload)
        self.transport.assert_not_called()

    def test_cli_help_and_offline_modes_and_no_overwrite(self):
        script = ROOT / "scripts" / "evaluate.py"
        help_result = subprocess.run([sys.executable, str(script), "--help"], cwd=ROOT,
                                     capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        for mode in ("next-action", "e2e"):
            output = self.directory / f"{mode}.json"
            command = [sys.executable, str(script), "--mode", mode,
                       "--input", str(ROOT / "data" / "samples" / f"evaluation-{mode}.json"),
                       "--output", str(output)]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            before = output.read_bytes()
            repeated = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(repeated.returncode, 2)
            self.assertEqual(before, output.read_bytes())
            self.assertIn("FileExistsError", repeated.stderr)

    def test_live_flag_without_approval_never_imports_transport(self):
        command = [sys.executable, str(ROOT / "scripts" / "evaluate.py"), "--mode", "e2e",
                   "--input", str(ROOT / "data" / "samples" / "evaluation-e2e.json"),
                   "--output", str(self.directory / "no-send.json"), "--send"]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires_--approval", result.stderr)

    def test_actual_approval_and_journal_prevent_immutable_attempt_resend(self):
        from foundry_distillation_lab.safety import Approval, Journal
        input_path = self.directory / "input.json"
        input_path.write_text('{"offline_test": true}', encoding="utf-8")
        approval_path = self.directory / "approval.json"
        approval_path.write_text(json.dumps({
            "approved": True, "operation": "eval-e2e",
            "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
            "target": "example-deployment", "max_requests": 10, "max_cost": 1, "currency": "USD",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }), encoding="utf-8")
        approval = Approval.load(approval_path, operation="eval-e2e", input_path=input_path)
        journal = Journal(self.directory)
        journal.start("attempt-1", self.payload)
        guarded = GuardedModel(self.transport, approval=approval, journal=journal,
                               target="example-deployment", reserve_per_request=0.1, send=True)
        with self.assertRaises(FileExistsError):
            guarded(self.payload)
        self.transport.assert_not_called()
        self.payload["attempt_id"] = "attempt-2"
        guarded(self.payload)
        self.assertEqual(self.transport.call_count, 1)
        with self.assertRaises(FileExistsError):
            guarded(self.payload)
        self.assertEqual(self.transport.call_count, 1)

    def test_openai_boundary_is_lazy_and_mockable_with_retries_disabled(self):
        client = Mock()
        client.chat.completions.create.return_value.model_dump.return_value = {
            "choices": [{"finish_reason": "stop", "message": {"content": "確認しました。", "tool_calls": []}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        }
        openai = Mock()
        openai.OpenAI.return_value = client
        identity = Mock()
        with patch.dict(sys.modules, {"openai": openai, "azure.identity": identity}):
            transport = OpenAITransport(base_url="https://example.invalid/openai/v1/")
            openai.OpenAI.assert_not_called()
            identity.DefaultAzureCredential.assert_not_called()
            response = transport({"target": "example-model", "messages": [], "tools": []})
        self.assertEqual(openai.OpenAI.call_args.kwargs["max_retries"], 0)
        self.assertIsNone(response["usage"]["cached_input_tokens"])
        self.assertEqual(response["usage"]["input_tokens"], 10)
        self.assertFalse(client.chat.completions.create.call_args.kwargs["store"])

    def test_dataset_cli_to_evaluation_bundle_without_fabricated_predictions(self):
        prepared = self.directory / "data"
        command = [sys.executable, str(ROOT / "scripts" / "prepare_data.py"),
                   "--input", str(ROOT / "data" / "samples" / "traces.jsonl"), "--output", str(prepared)]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        cases_path = prepared / "next-actions.jsonl"
        bundle_path = self.directory / "evaluation-input.json"
        command = [sys.executable, str(ROOT / "scripts" / "evaluate.py"),
                   "--mode", "next-action", "--prepare-next-actions",
                   "--input", str(cases_path), "--output", str(bundle_path)]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = loads(bundle_path.read_text(encoding="utf-8"))
        self.assertEqual(bundle["records"], [])
        self.assertEqual(bundle["source"]["sha256"], hashlib.sha256(cases_path.read_bytes()).hexdigest())
        self.assertFalse(bundle["source"]["quality_review_approved"])
        self.assertEqual(bundle["evidence_kind"], "unreviewed_cases_no_predictions")
        self.assertTrue(bundle["cases"])
        for case in bundle["cases"]:
            self.assertEqual(case["expected"], {"kind": case["kind"], "calls": case["ground_truth"]})
        from foundry_distillation_lab.evaluation import evaluate
        report = evaluate(bundle, "next-action")
        self.assertEqual(report["observed_records"], 0)
        self.assertEqual(report["overall"]["status_counts"]["technical_failure"], report["scheduled_slots"])
        self.assertEqual(report["overall"]["confirmed_business_successes"], 0)

    def test_preparation_cannot_enable_live_inference(self):
        command = [sys.executable, str(ROOT / "scripts" / "evaluate.py"),
                   "--mode", "next-action", "--prepare-next-actions", "--send",
                   "--input", str(ROOT / "data" / "samples" / "traces.jsonl"),
                   "--output", str(self.directory / "invalid.json")]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("without_live_flags", result.stderr)

    def test_live_binds_original_payload_bytes_not_changed_input_file(self):
        spec = importlib.util.spec_from_file_location("evaluation_cli_test", ROOT / "scripts" / "evaluate.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        bundle = loads((ROOT / "data" / "samples" / "evaluation-next-action.json").read_text(encoding="utf-8"))
        bundle["models"] = ["teacher"]
        bundle["records"] = []
        bundle["inference"] = {
            "targets": {"teacher": "example-deployment"}, "reserve_per_request": 0.1,
            "base_url": "https://example.invalid/openai/v1/",
        }
        approval = Mock()
        approval.data = {"input_sha256": "changed-file-digest"}
        args = SimpleNamespace(approval=self.directory / "approval.json", input=self.directory / "input.json",
                               run_dir=self.directory / "new-run", mode="next-action")
        with patch("foundry_distillation_lab.safety.Approval.load", return_value=approval):
            with patch.object(cli, "OpenAITransport") as transport:
                with self.assertRaisesRegex(ContractError, "changed_since_payload"):
                    cli.live(bundle, args, "original-parsed-bytes-digest")
                transport.assert_not_called()

    def test_guarded_cli_records_explicit_local_runtime_without_real_sdk(self):
        spec = importlib.util.spec_from_file_location("evaluation_cli_runtime_test", ROOT / "scripts" / "evaluate.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        bundle = loads((ROOT / "data" / "samples" / "evaluation-next-action.json").read_text(encoding="utf-8"))
        response = {"message": deepcopy(bundle["records"][0]["message"]), "usage": None}
        bundle.update(models=["teacher"], records=[])
        bundle["inference"] = {
            "targets": {"teacher": "example-deployment"}, "reserve_per_request": 0.1,
            "base_url": "https://example.invalid/openai/v1/",
        }
        input_path, approval_path = self.directory / "runtime-input.json", self.directory / "runtime-approval.json"
        input_path.write_text(json.dumps(bundle), encoding="utf-8")
        digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
        approval_path.write_text(json.dumps({
            "approved": True, "operation": "eval-next-action", "input_sha256": digest,
            "target": "example-deployment", "max_requests": 1, "max_cost": 1, "currency": "USD",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }), encoding="utf-8")
        args = SimpleNamespace(approval=approval_path, input=input_path, mode="next-action",
                               run_dir=self.directory / "runtime-run")
        transport = Mock(return_value=response)
        with patch.object(cli, "OpenAITransport", return_value=transport):
            captured = cli.live(bundle, args, digest)
        transport.assert_called_once()
        record = captured["records"][0]
        self.assertEqual(record["provenance"]["runtime"]["kind"], "local_direct_model")
        self.assertEqual(record["provenance"]["runtime"]["model"], "example-deployment")
        self.assertEqual(record["provenance"]["approved_input_sha256"], digest)
        from foundry_distillation_lab.evaluation import evaluate
        report = evaluate(captured, "next-action")
        # A mocked SDK response never turns this deliberately synthetic fixture into measurement.
        self.assertEqual(report["per_model"]["teacher"]["runtime_provenance"]["classification"], "synthetic")


if __name__ == "__main__":
    unittest.main()
