from copy import deepcopy
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from foundry_distillation_lab.evaluation import evaluate, evidence_sha256, import_hosted
from foundry_distillation_lab.evaluation.schema import ContractError, canonical, loads, parse_call


ROOT = Path(__file__).resolve().parents[1]


def sample(name):
    return loads((ROOT / "data" / "samples" / f"evaluation-hosted-{name}.json").read_text(encoding="utf-8"))


def native_fixture(capture):
    """Synthetic native-emitter schema fixture; not an observed Hosted run."""
    events = []
    for index, event in enumerate(capture["events"], 1):
        native = {"event": event["event"], "sequence": index, "conversation_id": capture["conversation_id"]}
        if event["event"] in ("model_start", "model_finish"):
            native["model_call_index"] = int(event["call_id"].split("-")[-1])
        if event["event"] == "model_finish":
            output = []
            message = event["message"]
            if message.get("content"):
                output.append({"type": "message", "role": "assistant",
                               "content": [{"type": "output_text", "text": message["content"]}]})
            for call in message.get("tool_calls", []):
                parsed = parse_call(call)
                output.append({"type": "function_call", "call_id": call["id"],
                               "name": parsed["name"], "arguments": canonical(parsed["arguments"])})
            native.update(status=event["status"], output=output, usage=None)
        if event["event"] in ("tool_start", "tool_finish"):
            native.update(tool_event_id=f"local-{event['call_id']}", provider_call_id=event["call_id"],
                          call_id=event["call_id"], framework_call_id_verified=True)
        if event["event"] == "tool_start":
            native.update(name=event["name"], arguments=deepcopy(event["arguments"]))
        if event["event"] == "tool_finish":
            native.update(status="returned", result=deepcopy(event["result"]))
        if event["event"] == "attempt_finish":
            native["status"] = "completed_unreviewed"
        events.append(native)
    return {
        "schema_version": 1, "kind": "hosted-conversation-evidence",
        "evidence_kind": "synthetic_illustration_not_measurement",
        "conversation_id": capture["conversation_id"], "user_input": capture["user_input"],
        "input_sha256": capture["input_sha256"], "tools": deepcopy(capture["tools"]),
        "source": {"target": capture["runtime"]["endpoint"] + "|" + capture["runtime"]["model"],
                   "collection_plan_sha256": "b" * 64},
        "runtime_identity": {"framework_agent_name": capture["agent"]["name"],
                             "package_versions": None, "retail_hashes": {}, "hosted_binding": None},
        "status": "completed_unreviewed", "elapsed_seconds": None, "events": events,
        "final_answer": capture["answer"], "final_state": deepcopy(capture["final_state"]),
        "final_state_available": capture["final_state"] is not None,
        "known_usage": [None], "actual_cost": None, "unknown_cost": True,
        "capture_files": [{"path": "illustrative.capture.jsonl", "sha256": "c" * 64}],
    }


class HostedAdapterTests(unittest.TestCase):
    def setUp(self):
        self.bundle = sample("cases")
        self.capture = sample("capture")
        self.digest = "a" * 64

    def imported(self):
        return import_hosted(self.bundle, [self.capture], "teacher", self.digest)

    def score(self):
        return evaluate(self.imported(), "e2e")["rows"][0]

    def test_complete_synthetic_fixture_is_only_review_pending(self):
        self.assertEqual(self.capture["evidence_kind"], "synthetic_illustration_not_measurement")
        self.assertEqual(self.score()["status"], "review_pending")
        self.assertFalse(self.score()["confirmed_business_success"])

    def test_runtime_input_and_agent_identity_preserved_in_report(self):
        self.capture["source"] = {"target": "illustrative-target", "collection_plan_sha256": "b" * 64}
        result = self.score()
        self.assertEqual(result["origin"], "hosted_capture_import")
        self.assertEqual(result["provenance"]["agent"], self.capture["agent"])
        self.assertEqual(result["provenance"]["runtime"], self.capture["runtime"])
        self.assertEqual(result["provenance"]["capture_source"], self.capture["source"])
        self.assertEqual(result["provenance"]["input_sha256"], self.capture["input_sha256"])
        self.assertEqual(result["provenance"]["source_sha256"], self.digest)
        self.assertEqual(result["provenance"]["identity_assurance"], "captured_metadata_not_remote_attestation")

    def test_unknown_usage_and_latency_remain_unknown(self):
        self.assertIsNone(self.score()["usage"]["input_tokens"])
        self.assertIsNone(self.score()["latency_seconds"])
        report = evaluate(self.imported(), "e2e")
        self.assertIsNone(report["overall"]["usage_total"]["input_tokens"])
        self.assertEqual(report["overall"]["latency_seconds"]["n"], 0)
        self.assertEqual(report["per_model"]["teacher"]["runtime_provenance"]["classification"], "synthetic")
        self.assertEqual(report["per_model"]["fine_tuned"]["runtime_provenance"]["classification"], "unknown")

    def test_runtime_classification_comes_from_records_not_planning_config(self):
        self.capture["evidence_kind"] = "captured_runtime_observation_unreviewed"
        report = evaluate(self.imported(), "e2e")
        self.assertEqual(report["per_model"]["teacher"]["runtime_provenance"]["classification"], "hosted_capture")
        self.assertEqual(report["per_model"]["base"]["runtime_provenance"]["classification"], "unknown")
        self.assertEqual(report["rows"][0]["evidence_kind"], "captured_runtime_observation_unreviewed")

    def test_missing_events_are_not_reconstructed_from_success_answer(self):
        del self.capture["events"]
        result = self.imported()
        self.assertIsNone(result["records"][0]["events"])
        self.assertEqual(self.score()["status"], "technical_failure")

    def test_missing_final_state_is_not_synthesized(self):
        del self.capture["final_state"]
        self.assertIsNone(self.imported()["records"][0]["final_state"])
        self.assertIn("final_state_not_supported_by_events", self.score()["technical_failures"])

    def test_missing_answer_is_not_synthesized_from_model_message(self):
        del self.capture["answer"]
        self.assertIsNone(self.imported()["records"][0]["answer"])
        self.assertEqual(self.score()["status"], "technical_failure")

    def test_model_only_responses_capture_is_explicitly_unsupported(self):
        self.capture = {"kind": "foundry-responses-capture", "request": {}, "response": {"status": "completed"}}
        with self.assertRaisesRegex(ContractError, "model_http_capture_is_insufficient"):
            self.imported()

    def test_input_hash_and_text_must_match_case(self):
        for key in ("input_sha256", "user_input", "conversation_id"):
            with self.subTest(key=key):
                original = self.capture[key]
                self.capture[key] = "wrong"
                with self.assertRaises(ContractError):
                    self.imported()
                self.capture[key] = original

    def test_agent_deployment_endpoint_and_tools_must_match_declared_target(self):
        for section, key in (("agent", "name"), ("runtime", "model"), ("runtime", "endpoint")):
            with self.subTest(key=key):
                original = self.capture[section][key]
                self.capture[section][key] = "wrong"
                with self.assertRaises(ContractError):
                    self.imported()
                self.capture[section][key] = original
        self.capture["tools"] = []
        with self.assertRaisesRegex(ContractError, "tool_contract_mismatch"):
            self.imported()

    def test_explicit_agent_resource_version_cannot_match_unknown(self):
        self.bundle["hosted_targets"]["teacher"]["agent_version"] = "version-1"
        with self.assertRaisesRegex(ContractError, "version_or_resource"):
            self.imported()

    def test_source_review_is_preserved_but_never_silently_rebound(self):
        review = {"decision": "confirmed_success", "reviewer": "example-human",
                  "reviewed_at": "2026-09-19T00:00:00Z", "notes": "Synthetic review fixture",
                  "evidence_sha256": evidence_sha256(self.capture)}
        self.capture["review"] = review
        result = self.imported()
        self.assertEqual(result["records"][0]["source_review"], review)
        self.assertNotIn("review", result["records"][0])
        self.assertEqual(self.score()["status"], "review_pending")

    def test_existing_reviewed_records_are_preserved(self):
        first = self.imported()
        first["records"][0]["model"] = "base"
        first["records"][0]["review"] = {
            "decision": "confirmed_success", "reviewer": "example-human",
            "reviewed_at": "2026-09-19T00:00:00Z", "notes": "Synthetic review fixture",
            "evidence_sha256": evidence_sha256(first["records"][0]),
        }
        self.bundle = first
        second = self.imported()
        self.assertEqual(second["records"][0], first["records"][0])
        self.assertEqual(evaluate(second, "e2e")["rows"][1]["status"], "confirmed_success")

    def test_duplicate_attempt_not_imported_as_new_denominator(self):
        self.bundle = self.imported()
        with self.assertRaisesRegex(ContractError, "duplicate_hosted"):
            self.imported()

    def test_scheduled_missing_variants_remain_in_denominator(self):
        report = evaluate(self.imported(), "e2e")
        self.assertEqual(report["scheduled_slots"], 3)
        self.assertEqual(report["observed_records"], 1)
        self.assertEqual(report["overall"]["status_counts"]["technical_failure"], 2)
        self.assertEqual(len({row["case_sha256"] for row in report["rows"]}), 1)
        self.assertEqual(len({row["tool_contract_sha256"] for row in report["rows"]}), 1)
        self.assertEqual(len(report["rows"][0]["evidence_sha256"]), 64)
        self.assertIsNone(report["rows"][1]["evidence_sha256"])

    def test_no_inputs_mutated(self):
        before_bundle, before_capture = deepcopy(self.bundle), deepcopy(self.capture)
        self.imported()
        self.assertEqual(self.bundle, before_bundle)
        self.assertEqual(self.capture, before_capture)

    def test_observed_tool_events_roundtrip_and_missing_start_fails(self):
        saved = loads((ROOT / "data" / "samples" / "evaluation-e2e.json").read_text(encoding="utf-8"))
        self.bundle["cases"] = saved["cases"]
        self.bundle["tools"] = saved["tools"]
        case, record = saved["cases"][0], saved["records"][0]
        self.capture.update(
            conversation_id=case["case_id"], user_input=case["user_input"],
            input_sha256=hashlib.sha256(case["user_input"].encode("utf-8")).hexdigest(),
            tools=deepcopy(saved["tools"]), events=deepcopy(record["events"]),
            answer=record["answer"], final_state=deepcopy(record["final_state"]),
            usage=deepcopy(record["usage"]),
        )
        for event in self.capture["events"]:
            if event["event"] in ("tool_start", "tool_finish"):
                event["framework_call_id_verified"] = True
        result = self.score()
        self.assertEqual(result["status"], "review_pending", result)
        self.assertEqual(result["usage"]["input_tokens"], 420)
        self.assertEqual(self.imported()["records"][0]["events"], self.capture["events"])
        del self.capture["events"][2]
        result = self.score()
        self.assertEqual(result["status"], "technical_failure")
        self.assertIn("unpaired_tool_finish", result["technical_failures"])

    def test_native_hosted_envelope_preserves_lineage_and_unknowns(self):
        self.capture = native_fixture(self.capture)
        result = self.score()
        self.assertEqual(result["status"], "review_pending")
        self.assertIsNone(result["usage"]["input_tokens"])
        self.assertEqual(result["provenance"]["capture_format"], "hosted-conversation-evidence")
        self.assertEqual(result["provenance"]["capture_files"], self.capture["capture_files"])
        self.assertEqual(result["provenance"]["runtime"]["native_identity"], self.capture["runtime_identity"])
        self.assertEqual(result["provenance"]["capture_sha256"],
                         hashlib.sha256(canonical(self.capture).encode("utf-8")).hexdigest())

    def test_native_null_final_state_never_becomes_completed_business_state(self):
        self.capture = native_fixture(self.capture)
        self.capture.update(final_state=None, final_state_available=False)
        self.assertIsNone(self.imported()["records"][0]["final_state"])
        self.assertEqual(self.score()["status"], "technical_failure")

    def test_native_inconsistent_state_availability_is_rejected(self):
        self.capture = native_fixture(self.capture)
        self.capture["final_state_available"] = False
        with self.assertRaisesRegex(ContractError, "availability_inconsistent"):
            self.imported()

    def test_native_provider_id_is_never_invented_from_local_event_id(self):
        saved = loads((ROOT / "data" / "samples" / "evaluation-e2e.json").read_text(encoding="utf-8"))
        self.bundle["cases"], self.bundle["tools"] = saved["cases"], saved["tools"]
        case, record = saved["cases"][0], saved["records"][0]
        self.capture.update(conversation_id=case["case_id"], user_input=case["user_input"],
                            input_sha256=hashlib.sha256(case["user_input"].encode("utf-8")).hexdigest(),
                            tools=deepcopy(saved["tools"]), events=deepcopy(record["events"]),
                            answer=record["answer"], final_state=deepcopy(record["final_state"]))
        self.capture = native_fixture(self.capture)
        self.assertEqual(self.score()["status"], "review_pending")
        self.capture["events"][2]["call_id"] = None
        self.capture["events"][3]["call_id"] = None
        self.capture["events"][2]["framework_call_id_verified"] = False
        self.capture["events"][3]["framework_call_id_verified"] = False
        imported = self.imported()["records"][0]
        self.assertIsNone(imported["events"][2]["call_id"])
        self.assertIsNone(imported["events"][3]["call_id"])
        self.assertIsNotNone(imported["events"][2]["provider_call_id"])
        self.assertEqual(self.score()["status"], "technical_failure")
        self.assertIn("unverified_framework_tool_call_linkage", self.score()["technical_failures"])


class HostedCliTests(unittest.TestCase):
    def test_import_then_score_and_refuse_overwrite(self):
        folder = ROOT / "runs" / f"evaluation-hosted-test-{uuid.uuid4().hex}"
        folder.mkdir(parents=True)
        try:
            script = ROOT / "scripts" / "evaluate.py"
            captured = ROOT / "data" / "samples" / "evaluation-hosted-capture.json"
            imported = folder / "imported.json"
            command = [sys.executable, str(script), "--mode", "e2e",
                       "--input", str(ROOT / "data" / "samples" / "evaluation-hosted-cases.json"),
                       "--import-hosted", str(captured), "--model-label", "teacher", "--output", str(imported)]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = loads(imported.read_text(encoding="utf-8"))
            self.assertEqual(data["imports"][0]["source_sha256"], hashlib.sha256(captured.read_bytes()).hexdigest())
            repeated = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(repeated.returncode, 2)
            scored = subprocess.run([sys.executable, str(script), "--mode", "e2e", "--input", str(imported),
                                     "--output", str(folder / "scores.json")],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(scored.returncode, 0, scored.stderr)
            report = json.loads((folder / "scores.json").read_text(encoding="utf-8"))
            self.assertEqual(report["rows"][0]["status"], "review_pending")
            self.assertEqual(report["evidence_kind"], "synthetic_illustration_not_measurement")
        finally:
            for attempt in range(10):
                try:
                    shutil.rmtree(folder)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.1)


class HostedEmitterIntegrationTests(unittest.TestCase):
    def test_model_and_tool_limits_block_before_extra_work(self):
        spec = importlib.util.spec_from_file_location(
            "hosted_capture_limits_test", ROOT / "deploy" / "hosted-agent" / "capture.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        endpoint = "https://example.invalid/api/projects/fixture"
        plan = {"target": endpoint + "|fixture", "config": {
            "project_endpoint": endpoint, "model": "fixture", "max_output_tokens": 128,
            "max_model_calls": 1, "max_tool_calls": 1}}
        selected = {"conversation_id": "limited-case", "category": "synthetic",
                    "prompt": "fixture", "input_sha256": hashlib.sha256(b"fixture").hexdigest()}

        class Request:
            method = "POST"
            url = endpoint + "/openai/v1/responses"

            async def aread(self):
                return canonical({"model": "fixture", "store": False, "max_output_tokens": 128,
                                  "input": [{"role": "user", "content": "fixture"}]}).encode()

        with tempfile.TemporaryDirectory() as directory:
            capture = module.Capture(plan, selected, directory)
            asyncio.run(capture.request(Request()))
            with self.assertRaisesRegex(RuntimeError, "Model call limit"):
                asyncio.run(capture.request(Request()))
            self.assertEqual(capture.counter, 1)
            self.assertEqual(len(list((Path(directory) / "attempts").glob("*.started.json"))), 1)
            capture.finish_unknown()

            called = []
            capture.provider_calls = [
                {"name": "get_order_details", "arguments": '{"order_id":"ORD-0001"}', "call_id": "first"},
                {"name": "get_order_details", "arguments": '{"order_id":"ORD-0002"}', "call_id": "second"}]
            capture.call_tool("get_order_details", {"order_id": "ORD-0001"}, lambda: called.append("first"))
            with self.assertRaisesRegex(RuntimeError, "Tool call limit"):
                capture.call_tool("get_order_details", {"order_id": "ORD-0002"}, lambda: called.append("second"))
            self.assertEqual(called, ["first"])
            self.assertTrue(capture.tool_failure)
            capture.finish("failed_or_unknown")
            evidence = loads((Path(directory) / f"{capture.prefix}.evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["status"], "unknown")
            self.assertIsNone(evidence["usage"]["input_tokens"])

    def test_call_limits_must_be_explicit_positive_integers(self):
        spec = importlib.util.spec_from_file_location(
            "hosted_capture_invalid_limits_test", ROOT / "deploy" / "hosted-agent" / "capture.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for value in (None, True, 0, -1, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.Capture({"config": {"max_model_calls": value, "max_tool_calls": 2}}, {}, ROOT)

    def test_actual_emitter_tool_events_import_without_reconstructing_missing_state(self):
        """Actual local collector, synthetic HTTP fixture: no Hosted service is run."""
        from foundry_distillation_lab.retail import RetailSession
        spec = importlib.util.spec_from_file_location(
            "hosted_capture_evaluation_test", ROOT / "deploy" / "hosted-agent" / "capture.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        folder = ROOT / "runs" / f"evaluation-emitter-test-{uuid.uuid4().hex}"
        folder.mkdir(parents=True)
        try:
            retail = RetailSession()
            endpoint, model = "https://example.invalid/api/projects/illustrative", "illustrative-teacher"
            user_input = "注文 ORD-0001 の配送状況だけ教えてください。変更は不要です。"
            selected = {"conversation_id": "synthetic-emitter-case", "category": "synthetic_fixture",
                        "prompt": user_input,
                        "input_sha256": hashlib.sha256(user_input.encode("utf-8")).hexdigest()}
            plan = {"target": endpoint + "|" + model, "config": {
                "project_endpoint": endpoint, "model": model, "max_output_tokens": 128,
                "max_model_calls": 12, "max_tool_calls": 24,
            }}
            capture = module.Capture(plan, selected, folder,
                                     {"framework_agent_name": "retail-trace-collector",
                                      "package_versions": {"fixture": "synthetic"},
                                      "retail_hashes": {}, "runtime_hashes": {}, "hosted_binding": None})
            capture.set_contract(retail.tools, retail.system_prompt)
            history = [{"role": "user", "content": user_input}]

            class Request:
                method = "POST"
                url = endpoint + "/openai/v1/responses"

                async def aread(self):
                    return canonical({"model": model, "store": False, "max_output_tokens": 128,
                                      "instructions": retail.system_prompt, "input": history}).encode("utf-8")

            class Response:
                def __init__(self, request, output, index):
                    self.request, self.output, self.index = request, output, index

                async def aread(self):
                    return canonical({"id": f"synthetic-response-{self.index}", "status": "completed",
                                      "output": self.output, "usage": None}).encode("utf-8")

            async def observe():
                for index, name in enumerate(("get_order_details", "get_fulfillment_status"), 1):
                    request = Request()
                    await capture.request(request)
                    arguments = {"order_id": "ORD-0001"}
                    call = {"type": "function_call", "call_id": f"actual-fixture-provider-id-{index}",
                            "name": name, "arguments": canonical(arguments)}
                    await capture.response(Response(request, [call], index))
                    result = capture.call_tool(name, arguments, lambda: retail.call(name, arguments))
                    history.extend([call, {"type": "function_call_output", "call_id": call["call_id"],
                                           "output": canonical(result)}])
                request = Request()
                await capture.request(request)
                answer = "注文 ORD-0001 は配達済みです。変更は行っていません。"
                await capture.response(Response(request, [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": answer}]}], 3))

            asyncio.run(observe())
            capture.finish("completed_unreviewed")
            artifact = folder / f"{capture.prefix}.evidence.json"
            evidence = loads(artifact.read_text(encoding="utf-8"))
            self.assertEqual(evidence["final_state_scope"], "observed_tool_returns_not_store_snapshot")
            self.assertEqual(evidence["final_state"], {"terminal": "answer_only", "submissions": []})
            bundle = {
                "schema": "retail-evaluation-input-v1", "evidence_kind": "synthetic_illustration_not_measurement",
                "models": ["teacher"], "tools": retail.tools, "records": [],
                "hosted_targets": {"teacher": {"agent_name": "retail-trace-collector",
                                                "model": model, "endpoint": endpoint}},
                "cases": [{"case_id": selected["conversation_id"], "category": "synthetic_fixture",
                           "user_input": user_input, "expected": {
                               "initial_tools": ["get_order_details", "get_fulfillment_status"],
                               "required_calls": [
                                   {"name": "get_order_details", "arguments": {"order_id": "ORD-0001"}},
                                   {"name": "get_fulfillment_status", "arguments": {"order_id": "ORD-0001"}}],
                               "allowed_mutations": [],
                               "final_state": {"terminal": "answer_only", "submissions": []},
                           }}],
            }
            imported = import_hosted(bundle, [evidence], "teacher",
                                     hashlib.sha256(artifact.read_bytes()).hexdigest())
            score = evaluate(imported, "e2e")
            self.assertEqual(score["evidence_kind"], "synthetic_illustration_not_measurement")
            self.assertEqual(score["rows"][0]["status"], "technical_failure", score["rows"][0])
            self.assertIn("unverified_framework_tool_call_linkage", score["rows"][0]["technical_failures"])
            self.assertFalse(score["rows"][0]["confirmed_business_success"])
            self.assertIsNone(score["rows"][0]["usage"]["input_tokens"])
            self.assertEqual(imported["records"][0]["events"], evidence["events"])
            self.assertEqual(capture.counter, 3)
            self.assertEqual(evidence["source"]["collection_plan_sha256"],
                             hashlib.sha256(canonical(plan).encode()).hexdigest())
            self.assertEqual(len(score["rows"][0]["provenance"]["capture_files"]), 3)
            for linked in evidence["capture_files"]:
                self.assertEqual(linked["sha256"], hashlib.sha256((folder / linked["path"]).read_bytes()).hexdigest())
        finally:
            for attempt in range(10):
                try:
                    shutil.rmtree(folder)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
