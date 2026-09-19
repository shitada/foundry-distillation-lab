import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from foundry_distillation_lab.io import read_json, sha256, write_json, write_jsonl
from foundry_distillation_lab.retail import RetailSession
from foundry_distillation_lab.training import collection, invocation, jobs
from foundry_distillation_lab.training.execution import execute_once, project_endpoint


class Workspace(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / "runs" / ("test-training-" + uuid.uuid4().hex)
        self.work.mkdir(parents=True)

    def tearDown(self):
        # OneDrive/Windows indexing can briefly hold newly closed evidence files.
        for retry in range(6):
            try:
                shutil.rmtree(self.work)
                return
            except PermissionError:
                if retry == 5:
                    raise
                time.sleep(0.1 * 2 ** retry)

    def approval(self, source, operation, target, requests=5, cost=100):
        path = self.work / ("approval-" + uuid.uuid4().hex + ".json")
        write_json(path, {"approved": True, "operation": operation,
                         "input_sha256": sha256(source), "target": target,
                         "max_requests": requests, "max_cost": cost, "currency": "JPY",
                         "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        return path


class FakeTransport:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def upload(self, name, raw):
        self.calls.append(("upload", name, raw))
        if self.fail:
            raise TimeoutError("not logged")
        return {"id": "file-" + name.split(".")[0], "status": "processed"}

    def submit(self, payload):
        self.calls.append(("submit", payload))
        return {"id": "ftjob-test", "status": "pending"}

    def status(self, kind, identifier):
        self.calls.append(("GET", kind, identifier))
        return {"id": identifier, "status": "succeeded", "trained_tokens": 123}


class TrainingTests(Workspace):
    def prepare(self):
        train = self.work / "train.jsonl"
        val = self.work / "validation.jsonl"
        row = lambda n: {"messages": [{"role": "user", "content": f"問い合わせ {n}"},
                                     {"role": "assistant", "content": f"回答 {n}"}]}
        write_jsonl(train, [row(n) for n in range(10)])
        write_jsonl(val, [row(11)])
        config = self.work / "config.json"
        write_json(config, {"project_endpoint": "https://example.services.ai.azure.com/api/projects/lab",
                            "model": "test-model-version", "training_type": "Standard",
                            "hyperparameters": {"n_epochs": 2}, "upload_estimated_cost": 0,
                            "submit_estimated_cost": 10})
        jobs.prepare(train, val, config, self.work / "prepared")
        return self.work / "prepared" / "upload-plan.json"

    def test_prepare_hashes_bom_and_create_only(self):
        plan_path = self.prepare()
        plan = read_json(plan_path)
        raw = (plan_path.parent / "train.jsonl").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(plan["files"]["train"]["sha256"], sha256(plan_path.parent / "train.jsonl"))
        with self.assertRaises(FileExistsError):
            jobs.prepare(self.work / "train.jsonl", self.work / "validation.jsonl",
                         self.work / "config.json", plan_path.parent)

    def test_upload_requires_execute_and_exact_approval(self):
        path = self.prepare()
        fake = FakeTransport()
        with self.assertRaises(ValueError):
            jobs.upload(path, "train", run_dir=self.work, transport=fake)
        approval = self.approval(path, "training-upload", "wrong")
        with self.assertRaises(ValueError):
            jobs.upload(path, "train", execute=True, approval_path=approval,
                        run_dir=self.work, transport=fake)
        self.assertEqual(fake.calls, [])

    def test_modified_dataset_rejected(self):
        path = self.prepare()
        approval = self.approval(path, "training-upload", read_json(path)["target"])
        with (path.parent / "train.jsonl").open("ab") as handle:
            handle.write(b" ")
        fake = FakeTransport()
        with self.assertRaises(ValueError):
            jobs.upload(path, "train", execute=True, approval_path=approval,
                        run_dir=self.work, transport=fake)
        self.assertFalse(fake.calls)

    def test_unknown_upload_cannot_be_replayed(self):
        path = self.prepare()
        approval = self.approval(path, "training-upload", read_json(path)["target"])
        fake = FakeTransport(fail=True)
        options = dict(execute=True, approval_path=approval, run_dir=self.work, transport=fake)
        with self.assertRaises(TimeoutError):
            jobs.upload(path, "train", **options)
        with self.assertRaises(FileExistsError):
            jobs.upload(path, "train", **options)
        self.assertEqual(len(fake.calls), 1)
        result = read_json(next((self.work / "attempts").glob("*.result.json")))
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertIsNone(result["result"]["actual_cost"])

    def test_upload_submit_get_status_flow(self):
        path = self.prepare()
        target = read_json(path)["target"]
        approval = self.approval(path, "training-upload", target, requests=2)
        fake = FakeTransport()
        for role in ("train", "validation"):
            jobs.upload(path, role, execute=True, approval_path=approval,
                        run_dir=self.work, transport=fake)
        submit_path = self.work / "submit.json"
        plan = jobs.prepare_submission(path, self.work / "train-upload-receipt.json",
                                       self.work / "validation-upload-receipt.json", submit_path)
        self.assertEqual(plan["payload"]["training_file"], "file-train")
        self.assertEqual(plan["payload"]["method"]["type"], "supervised")
        submit_approval = self.approval(submit_path, "training-submit", target, requests=1)
        jobs.submit(submit_path, execute=True, approval_path=submit_approval,
                    run_dir=self.work, transport=fake)
        receipt = self.work / "job-receipt.json"
        status_approval = self.approval(receipt, "training-submit", target, requests=1)
        result = jobs.status(receipt, execute=True, approval_path=status_approval,
                             run_dir=self.work, observation_id="one", transport=fake)
        self.assertEqual(result["trained_tokens"], 123)
        self.assertEqual(fake.calls[-1], ("GET", "job", "ftjob-test"))

    def test_budget_prevents_transport(self):
        path = self.prepare()
        approval = self.approval(path, "training-submit", read_json(path)["target"], cost=1)
        sent = []
        with self.assertRaises(ValueError):
            execute_once(execute=True, approval_path=approval, operation="training-submit",
                input_path=path, target_id=read_json(path)["target"], run_dir=self.work,
                payload={}, estimated_cost=2, send=lambda: sent.append(True))
        self.assertFalse(sent)

    def test_request_limit_is_shared_between_uploads(self):
        path = self.prepare()
        approval = self.approval(path, "training-upload", read_json(path)["target"], requests=1)
        fake = FakeTransport()
        jobs.upload(path, "train", execute=True, approval_path=approval,
                    run_dir=self.work, transport=fake)
        with self.assertRaises(ValueError):
            jobs.upload(path, "validation", execute=True, approval_path=approval,
                        run_dir=self.work, transport=fake)
        self.assertEqual(len(fake.calls), 1)

    def test_expired_and_changed_plan_approvals_rejected(self):
        path = self.prepare()
        approval_path = self.approval(path, "training-upload", read_json(path)["target"])
        approval = read_json(approval_path)
        approval["expires_at"] = "2000-01-01T00:00:00Z"
        approval_path.write_text(json.dumps(approval), encoding="utf-8")
        fake = FakeTransport()
        with self.assertRaises(ValueError):
            jobs.upload(path, "train", execute=True, approval_path=approval_path,
                        run_dir=self.work, transport=fake)
        fresh = self.approval(path, "training-upload", read_json(path)["target"])
        plan = read_json(path)
        plan["config"]["hyperparameters"]["n_epochs"] = 10
        path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaises(ValueError):
            jobs.upload(path, "train", execute=True, approval_path=fresh,
                        run_dir=self.work, transport=fake)
        self.assertFalse(fake.calls)

    def test_endpoint_rejects_credentials_redirect_targets(self):
        for url in ("https://example.org/api/projects/lab",
                    "http://example.services.ai.azure.com/api/projects/lab",
                    "https://key@example.services.ai.azure.com/api/projects/lab",
                    "https://example.services.ai.azure.com/api/projects/lab?override=1"):
            with self.assertRaises(ValueError):
                project_endpoint(url)

    def test_no_metadata_in_sft(self):
        with self.assertRaises(ValueError):
            jobs.validate_rows(b'{"messages":[],"oracle":"never upload"}')
        with self.assertRaises(ValueError):
            jobs.training_config({"project_endpoint": "https://example.services.ai.azure.com/api/projects/lab",
                "model": "test", "training_type": "Standard", "hyperparameters": {"batch_size": "auto"},
                "upload_estimated_cost": 0, "submit_estimated_cost": 10})


class CollectionTests(Workspace):
    def conversation(self):
        retail = RetailSession()
        return {"conversation_id": "sample-one", "category": "返品", "tools": retail.tools,
                "messages": [{"role": "system", "content": retail.system_prompt},
                    {"role": "user", "content": "ORD-001 の返品について"},
                    {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "get_order_details", "arguments": '{"order_id":"ORD-001"}'}}]},
                    {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
                    {"role": "assistant", "content": "状況を確認してください。"}]}

    def test_import_preserves_unknown_usage_and_requires_actual_rows(self):
        source = self.work / "export.jsonl"
        row = self.conversation()
        row.update(model="observed-teacher", source={"export_id": "example-export"})
        write_jsonl(source, [row])
        result = collection.import_traces(source, self.work / "imported", "conversation")
        self.assertEqual(result["cloud_requests_made"], 0)
        self.assertIsNone(result["observations"][0]["known_usage"])
        self.assertTrue(result["observations"][0]["unknown_cost"])
        self.assertEqual(result["observations"][0]["model"], "observed-teacher")
        self.assertEqual(result["observations"][0]["source"], {"export_id": "example-export"})
        self.assertTrue((self.work / "imported" / "traces.jsonl").exists())

    def test_import_never_relabels_scripted_source_as_cloud_observation(self):
        source = self.work / "scripted.jsonl"
        row = self.conversation()
        row["source_kind"] = "scripted_illustration_not_teacher_output"
        write_jsonl(source, [row])
        result = collection.import_traces(source, self.work / "scripted-import", "conversation")
        self.assertEqual(result["source_kinds"], ["scripted_illustration_not_teacher_output"])

    def test_responses_capture_converts_tool_history(self):
        retail = RetailSession()
        captured = {"kind": "foundry-responses-capture", "conversation_id": "one",
            "category": "返品", "call_index": 1,
            "request": {"model": "teacher", "instructions": retail.system_prompt,
                "tools": [{"type": "function", **t["function"]} for t in retail.tools],
                "input": [{"role": "user", "content": "ORD-001 の返品について"},
                    {"type": "function_call", "call_id": "call_1", "name": "get_order_details",
                     "arguments": '{"order_id":"ORD-001"}'},
                    {"type": "function_call_output", "call_id": "call_1", "output": "{}"}]},
            "response": {"id": "resp_one", "status": "completed", "usage": {"input_tokens": 5},
                         "output": [{"type": "message", "role": "assistant",
                                     "content": [{"type": "output_text", "text": "確認しました。"}]}]}}
        source = self.work / "capture.jsonl"
        write_jsonl(source, [captured])
        result = collection.import_traces(source, self.work / "converted", "responses-capture")
        self.assertEqual(result["conversations"], 1)
        captured["request"]["previous_response_id"] = "resp_previous"
        with self.assertRaises(ValueError):
            collection.convert_capture([captured])

    def test_collection_plan_offline(self):
        source = self.work / "prompts.jsonl"
        write_jsonl(source, [{"conversation_id": "one", "category": "返品", "prompt": "返品です"}])
        plan = collection.prepare_collection(source, ROOT / "configs" / "examples" /
                                             "cloud-collection.json", self.work / "plan")
        self.assertEqual(plan["kind"], "collection")
        self.assertEqual(len(plan["inputs"][0]["input_sha256"]), 64)

    def test_staging_allowlist_and_sse_parser(self):
        here = ROOT / "deploy" / "hosted-agent"
        def load(name):
            spec = importlib.util.spec_from_file_location(name, here / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        files = load("stage").stage(self.work / "build")
        self.assertEqual(files["LICENSE"], sha256(ROOT / "LICENSE"))
        self.assertEqual((self.work / "build" / "LICENSE").read_bytes(), (ROOT / "LICENSE").read_bytes())
        self.assertTrue(any("retail" in f for f in files))
        self.assertFalse(any("evaluation" in f or "dataset" in f or "cases" in f for f in files))
        event = b'data: {"type":"response.completed","response":{"id":"resp_test"}}\n\ndata: [DONE]\n'
        self.assertEqual(load("capture").terminal_response(event)["id"], "resp_test")
        with self.assertRaises(ValueError):
            load("capture").terminal_response(b"data: [DONE]\n")

    def test_hosted_capture_journals_before_send_and_rejects_replay(self):
        from foundry_distillation_lab.safety import Approval
        spec = importlib.util.spec_from_file_location("hosted_capture",
                                                     ROOT / "deploy" / "hosted-agent" / "capture.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        endpoint = "https://example.services.ai.azure.com/api/projects/lab"
        plan = {"config": {"project_endpoint": endpoint, "model": "teacher",
                          "max_output_tokens": 10, "estimated_cost_per_model_request": 1}}
        source = self.work / "plan.json"
        write_json(source, plan)
        approval = Approval.load(self.approval(source, "collect", endpoint + "|teacher"), "collect", source)
        selected = {"conversation_id": "one", "category": "返品"}
        capture = module.Capture(plan, selected, approval, self.work)

        class Request:
            method = "POST"
            url = endpoint + "/openai/v1/responses"

            async def aread(self):
                return json.dumps({"model": "teacher", "store": False,
                                   "max_output_tokens": 10, "input": []}).encode()

        request = Request()
        asyncio.run(capture.request(request))
        self.assertEqual(len(list((self.work / "attempts").glob("*.started.json"))), 1)
        capture.finish_unknown()
        restarted = module.Capture(plan, selected, approval, self.work)
        with self.assertRaises(FileExistsError):
            asyncio.run(restarted.request(request))

    def test_hosted_evidence_contains_observed_tools_and_no_invented_state(self):
        from foundry_distillation_lab.safety import Approval
        spec = importlib.util.spec_from_file_location("evidence_capture",
                                                     ROOT / "deploy" / "hosted-agent" / "capture.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        endpoint = "https://example.services.ai.azure.com/api/projects/lab"
        plan = {"target": endpoint + "|teacher", "config": {
            "project_endpoint": endpoint, "model": "teacher",
            "max_output_tokens": 10, "estimated_cost_per_model_request": 1}}
        selected = {"conversation_id": "one", "category": "返品",
                    "input_sha256": hashlib.sha256("返品".encode()).hexdigest(), "prompt": "返品"}
        source = self.work / "plan.json"
        write_json(source, plan)
        approval = Approval.load(self.approval(source, "collect", plan["target"]), "collect", source)
        capture = module.Capture(plan, selected, approval, self.work,
                                 runtime_identity={"framework_agent_name": "mock-test"})

        class Request:
            method = "POST"
            url = endpoint + "/openai/v1/responses"

            async def aread(self):
                return json.dumps({"model": "teacher", "store": False,
                                   "max_output_tokens": 10, "input": []}).encode()

        class Response:
            def __init__(self, request, output):
                self.request, self.output = request, output

            async def aread(self):
                return json.dumps({"id": "resp-observed", "status": "completed",
                                   "output": self.output, "usage": None}).encode()

        capture.event("attempt_start", input_sha256=selected["input_sha256"])
        request = Request()
        asyncio.run(capture.request(request))
        asyncio.run(capture.response(Response(request, [{"type": "function_call",
            "call_id": "call-observed", "name": "get_order_details", "arguments": '{"order_id":"ORD-001"}'}])))
        retail = RetailSession()
        capture.set_contract(retail.tools, retail.system_prompt)
        result = capture.call_tool("get_order_details", {"order_id": "ORD-001"},
                                   lambda: retail.call("get_order_details", {"order_id": "ORD-001"}))
        self.assertEqual(result["order_id"], "ORD-001")
        request = Request()
        asyncio.run(capture.request(request))
        asyncio.run(capture.response(Response(request, [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "注文を確認しました。"}]}])))
        evidence = capture.finish("completed_unreviewed")
        self.assertEqual(evidence["kind"], "hosted-retail-evidence")
        self.assertNotIn("attempt_start", {event["event"] for event in evidence["events"]})
        self.assertEqual(evidence["final_answer"], "注文を確認しました。")
        self.assertEqual(evidence["final_state"], {"terminal": "answer_only", "submissions": []})
        self.assertTrue(evidence["final_state_available"])
        self.assertEqual(evidence["final_state_scope"], "observed_tool_returns_not_store_snapshot")
        self.assertEqual(evidence["user_input"], "返品")
        self.assertEqual(evidence["tools"], retail.tools)
        self.assertEqual(evidence["runtime"]["system_prompt_sha256"],
                         hashlib.sha256(retail.system_prompt.encode()).hexdigest())
        self.assertIsNone(evidence["runtime"]["sdk_versions"])
        start = next(event for event in evidence["events"] if event["event"] == "tool_start")
        self.assertEqual(start["provider_call_id"], "call-observed")
        self.assertEqual(start["call_id"], "call-observed")
        self.assertEqual(start["call_id_source"], "unique_unconsumed_observed_proposal")
        self.assertEqual(evidence["known_usage"], [None, None])
        self.assertEqual(len(evidence["capture_files"]), 2)
        self.assertTrue(list(self.work.glob("*.evidence.json")))

    def test_hosted_tool_failure_stops_execution_and_ambiguous_ids_stay_unknown(self):
        spec = importlib.util.spec_from_file_location("failure_capture",
                                                     ROOT / "deploy" / "hosted-agent" / "capture.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        capture = module.Capture({"target": "test"}, {"conversation_id": "one", "category": "返品",
            "input_sha256": "0" * 64}, SimpleNamespace(data={"input_sha256": "1" * 64}), self.work)
        capture.provider_calls = [{"call_id": identifier, "name": "get_order_details",
                                   "arguments": '{"order_id":"ORD-001"}'} for identifier in ("one", "two")]
        with self.assertRaises(RuntimeError):
            capture.call_tool("get_order_details", {"order_id": "ORD-001"},
                              lambda: self.fail("ambiguous proposal must not execute"))
        self.assertIsNone(capture.events[0]["call_id"])
        self.assertEqual(capture.events[0]["event"], "tool_blocked")
        self.assertIsNone(capture.finish("failed_or_unknown")["final_state"])

        capture = module.Capture({"target": "test"}, {"conversation_id": "two", "category": "返品",
            "input_sha256": "0" * 64}, SimpleNamespace(data={"input_sha256": "1" * 64}), self.work)
        capture.provider_calls = [{"call_id": "observed-submit", "name": "submit_resolution",
                                   "arguments": "{}"}]

        def failed():
            raise ValueError("Mock uncertain tool failure")

        with self.assertRaises(ValueError):
            capture.call_tool("submit_resolution", {}, failed)
        with self.assertRaises(RuntimeError):
            asyncio.run(capture.request(object()))
        with self.assertRaises(RuntimeError):
            capture.call_tool("get_order_details", {}, lambda: self.fail("must not execute"))
        evidence = capture.finish("failed_or_unknown")
        self.assertIsNone(evidence["final_answer"])
        self.assertEqual(evidence["events"][-2]["status"], "unknown")
        self.assertEqual(evidence["events"][-2]["observed_status"], "exception_unknown")


class InvocationTests(Workspace):
    def prepare(self):
        source = self.work / "prompts.jsonl"
        write_jsonl(source, [{"conversation_id": "one", "category": "返品", "prompt": "返品です"}])
        collection.prepare_collection(source, ROOT / "configs" / "examples" /
                                      "cloud-collection.json", self.work / "collection")
        config = read_json(ROOT / "configs" / "examples" / "cloud-invocation.json")
        config["conversation_id"] = "one"
        config_path = self.work / "invoke-config.json"
        write_json(config_path, config)
        invocation.prepare(self.work / "collection" / "collection-plan.json",
                           config_path, self.work / "invocation")
        return self.work / "invocation" / "invocation-plan.json"

    def test_prepare_pins_version_prompt_and_current_cli_flags(self):
        plan = read_json(self.prepare())
        args = invocation.command(plan)
        self.assertIn("--new-session", args)
        self.assertNotIn("--new-conversation", args)
        self.assertIn("--no-prompt", args)
        self.assertIn(plan["agent_endpoint"], args)
        self.assertEqual(plan["input"]["prompt"], "返品です")
        plan["agent_endpoint"] = "https://wrong.example/agents/one/versions/1"
        with self.assertRaises(ValueError):
            invocation.validate(plan)

    def test_guarded_invocation_no_replay_or_faux_capture_success(self):
        path = self.prepare()
        plan = read_json(path)
        approval = self.approval(path, "collect", plan["target"], requests=1)

        class Fake:
            calls = 0

            def invoke(self, plan):
                self.calls += 1
                return {"http_status": 200, "response": {"status": "completed", "id": "resp-one"},
                        "capture_retrieval": "not_verified"}

        fake = Fake()
        with self.assertRaises(ValueError):
            invocation.invoke(path, run_dir=self.work, transport=fake)
        self.assertEqual(fake.calls, 0)
        receipt = invocation.invoke(path, execute=True, approval_path=approval,
                                    run_dir=self.work, transport=fake)
        self.assertFalse(receipt["trace_capture_verified"])
        with self.assertRaises(FileExistsError):
            invocation.invoke(path, execute=True, approval_path=approval,
                              run_dir=self.work, transport=fake)
        self.assertEqual(fake.calls, 1)

    def test_parse_raw_response_never_keeps_headers(self):
        parsed = invocation.parse_response(
            'HTTP/1.1 200 OK\r\nSome-Header: do-not-persist\r\n\r\n'
            '{"id":"resp-one","status":"completed","usage":{"input_tokens":1}}')
        self.assertEqual(parsed["usage"], {"input_tokens": 1})
        self.assertNotIn("do-not-persist", json.dumps(parsed))
        with self.assertRaises(ValueError):
            invocation.parse_response("request timed out")


if __name__ == "__main__":
    unittest.main()
