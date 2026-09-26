import asyncio
import importlib.util
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import time
import unittest
from unittest.mock import patch
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

class FakeTransport:
    def __init__(self, fail=False, file_states=None, job_state="succeeded", submit_fail=False):
        self.calls = []
        self.fail = fail
        self.submit_fail = submit_fail
        self.file_states = file_states or {}
        self.job_state = job_state

    def upload(self, name, raw):
        self.calls.append(("upload", name, raw))
        if self.fail:
            raise TimeoutError("not logged")
        return {"id": "file-" + name.split(".")[0], "status": "uploaded"}

    def submit(self, payload):
        self.calls.append(("submit", payload))
        if self.submit_fail:
            raise TimeoutError("uncertain submission")
        return {"id": "ftjob-test", "status": "pending"}

    def status(self, kind, identifier):
        self.calls.append(("GET", kind, identifier))
        if kind == "file":
            states = self.file_states.get(identifier, ["processed"])
            state = states.pop(0) if len(states) > 1 else states[0]
            return {"id": identifier, "status": state}
        return {"id": identifier, "status": self.job_state, "trained_tokens": 123}


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class TrainingTests(Workspace):
    def inputs(self):
        train = self.work / "train.jsonl"
        val = self.work / "validation.jsonl"
        row = lambda n: {"messages": [{"role": "user", "content": f"問い合わせ {n}"},
                                     {"role": "assistant", "content": f"回答 {n}"}]}
        write_jsonl(train, [row(n) for n in range(10)])
        write_jsonl(val, [row(11)])
        config = self.work / "config.json"
        write_json(config, {"project_endpoint": "https://example.services.ai.azure.com/api/projects/lab",
                            "model": "test-model-version", "training_type": "Standard",
                            "hyperparameters": {"n_epochs": 2}})
        return train, val, config

    def prepare(self):
        train, val, config = self.inputs()
        jobs.prepare(train, val, config, self.work / "prepared")
        return self.work / "prepared" / "upload-plan.json"

    def start(self, fake=None, **kwargs):
        return jobs.start(*self.inputs(), self.work / "training",
                          transport=fake or FakeTransport(), **kwargs)

    def test_prepare_hashes_bom_and_create_only(self):
        plan_path = self.prepare()
        plan = read_json(plan_path)
        raw = (plan_path.parent / "train.jsonl").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(plan["files"]["train"]["sha256"], sha256(plan_path.parent / "train.jsonl"))
        with self.assertRaises(FileExistsError):
            jobs.prepare(self.work / "train.jsonl", self.work / "validation.jsonl",
                         self.work / "config.json", plan_path.parent)

    def test_modified_dataset_rejected(self):
        path = self.prepare()
        with (path.parent / "train.jsonl").open("ab") as handle:
            handle.write(b" ")
        fake = FakeTransport()
        with self.assertRaises(ValueError):
            jobs.upload(path, "train", run_dir=self.work, transport=fake)
        self.assertFalse(fake.calls)

    def test_unknown_upload_cannot_be_replayed(self):
        path = self.prepare()
        fake = FakeTransport(fail=True)
        options = dict(run_dir=self.work, transport=fake)
        with self.assertRaises(TimeoutError):
            jobs.upload(path, "train", **options)
        with self.assertRaises(FileExistsError):
            jobs.upload(path, "train", **options)
        self.assertEqual(len(fake.calls), 1)
        result = read_json(next((self.work / "attempts").glob("*.result.json")))
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertIsNone(result["result"]["actual_cost"])

    def test_start_submit_repeatable_status_and_no_replay(self):
        fake = FakeTransport()
        receipt = self.start(fake)
        run = self.work / "training"
        plan = read_json(run / "submit-plan.json")
        self.assertEqual(plan["payload"]["training_file"], "file-train")
        self.assertEqual(plan["payload"]["method"]["type"], "supervised")
        self.assertEqual(receipt["response"]["id"], "ftjob-test")
        for _ in range(2):
            result = jobs.status(run, transport=fake)
            self.assertEqual(result["outcome"], "succeeded")
            self.assertEqual(result["states"]["job"]["trained_tokens"], 123)
        self.assertEqual(fake.calls[-1], ("GET", "job", "ftjob-test"))
        with self.assertRaises(FileExistsError):
            jobs.start(self.work / "train.jsonl", self.work / "validation.jsonl",
                       self.work / "config.json", run, transport=fake)
        with self.assertRaises(FileExistsError):
            jobs.submit(run / "submit-plan.json", run_dir=run, transport=fake)
        self.assertEqual(sum(call[0] == "submit" for call in fake.calls), 1)
        self.assertEqual(sum(call[0] == "upload" for call in fake.calls), 2)
        self.assertEqual(len(list((run / "observations").glob("*.json"))), 4)

    def test_changed_config_plan_rejected_before_transport(self):
        path = self.prepare()
        fake = FakeTransport()
        plan = read_json(path)
        plan["config"]["hyperparameters"]["n_epochs"] = 10
        path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaises(ValueError):
            jobs.upload(path, "train", run_dir=self.work, transport=fake)
        self.assertFalse(fake.calls)

    def test_both_files_must_be_processed_before_submit(self):
        clock = Clock()
        fake = FakeTransport(file_states={"file-train": ["processed"],
                                         "file-validation": ["uploaded", "pending", "processed"]})
        self.start(fake, clock=clock, sleep=clock.sleep, timeout_seconds=20, poll_seconds=2)
        self.assertEqual(clock.now, 4)
        self.assertEqual([call[0] for call in fake.calls],
                         ["upload", "upload", "GET", "GET", "GET", "GET", "submit"])

    def test_file_processing_error_never_submits(self):
        fake = FakeTransport(file_states={"file-validation": ["error"]})
        with self.assertRaisesRegex(ValueError, "processing failed"):
            self.start(fake)
        self.assertFalse(any(call[0] == "submit" for call in fake.calls))
        self.assertEqual(jobs.status(self.work / "training", transport=fake)["outcome"], "failed")

    def test_file_processing_deadline_and_interrupted_status(self):
        clock = Clock()
        fake = FakeTransport(file_states={"file-validation": ["uploaded"]})
        with self.assertRaises(TimeoutError):
            self.start(fake, clock=clock, sleep=clock.sleep, timeout_seconds=3, poll_seconds=2)
        self.assertEqual(clock.now, 3)
        self.assertFalse(any(call[0] == "submit" for call in fake.calls))
        result = jobs.status(self.work / "training", transport=fake)
        self.assertEqual(result["outcome"], "no_job")
        self.assertIn("do not blindly", result["message"])
        self.assertEqual(set(result["states"]), {"train", "validation"})

    def test_unknown_submission_never_resends(self):
        fake = FakeTransport(submit_fail=True)
        with self.assertRaises(TimeoutError):
            self.start(fake)
        run = self.work / "training"
        with self.assertRaises(FileExistsError):
            jobs.submit(run / "submit-plan.json", run_dir=run, transport=fake)
        self.assertEqual(jobs.status(run, transport=fake)["outcome"], "no_job")
        self.assertEqual(sum(call[0] == "submit" for call in fake.calls), 1)
        evidence = read_json(next((run / "attempts").glob("training-submit-*.result.json")))
        self.assertEqual(evidence["status"], "outcome_unknown")

    def test_lost_job_receipt_reconciled_from_journal_without_mutation(self):
        fake = FakeTransport()
        def save(path, value):
            if Path(path).name == "job-receipt.json":
                raise OSError("simulated disk failure")
            return write_json(path, value)
        with patch.object(jobs, "write_json", side_effect=save):
            with self.assertRaises(OSError):
                self.start(fake)
        run = self.work / "training"
        self.assertFalse((run / "job-receipt.json").exists())
        self.assertEqual(jobs.status(run, transport=fake)["outcome"], "succeeded")
        (run / "job-receipt.json").write_text("{", encoding="utf-8")
        result = jobs.status(run, transport=fake)
        self.assertEqual(result["outcome"], "succeeded")
        self.assertEqual(result["diagnostics"][0]["path"], str(run / "job-receipt.json"))
        self.assertIn("JSONDecodeError", result["diagnostics"][0]["reason"])
        self.assertEqual(sum(call[0] == "submit" for call in fake.calls), 1)
        with self.assertRaises(FileExistsError):
            jobs.submit(run / "submit-plan.json", run_dir=run, transport=fake)

    def test_lost_journal_save_still_blocks_resend(self):
        fake = FakeTransport()
        def save(path, value):
            if Path(path).name.startswith("training-submit-") and str(path).endswith(".result.json"):
                Path(path).write_text("{", encoding="utf-8")
                raise OSError("simulated journal disk failure")
            return write_json(path, value)
        with patch("foundry_distillation_lab.safety.write_json", side_effect=save):
            with self.assertRaises(OSError):
                self.start(fake)
        run = self.work / "training"
        result = jobs.status(run, transport=fake)
        self.assertEqual(result["outcome"], "no_job")
        self.assertIn("training-submit-", result["diagnostics"][0]["path"])
        self.assertIn("JSONDecodeError", result["diagnostics"][0]["reason"])
        with self.assertRaises(FileExistsError):
            jobs.submit(run / "submit-plan.json", run_dir=run, transport=fake)
        self.assertEqual(sum(call[0] == "submit" for call in fake.calls), 1)

    def test_unreadable_journal_reports_path_before_receipt_fallback(self):
        fake = FakeTransport()
        self.start(fake)
        run = self.work / "training"
        journal_path = next((run / "attempts").glob("training-submit-*.result.json"))
        def read(path):
            if Path(path) == journal_path:
                raise PermissionError("simulated unreadable evidence")
            return read_json(path)
        with patch.object(jobs, "read_json", side_effect=read):
            result = jobs.status(run, transport=fake)
        self.assertEqual(result["outcome"], "succeeded")
        self.assertEqual(result["diagnostics"], [{
            "path": str(journal_path), "reason": "PermissionError: simulated unreadable evidence"}])
        self.assertEqual(sum(call[0] == "submit" for call in fake.calls), 1)

    def test_malformed_journal_reports_diagnostics_with_receipt_fallback(self):
        fake = FakeTransport()
        self.start(fake)
        run = self.work / "training"
        journal_path = next((run / "attempts").glob("training-submit-*.result.json"))
        malformed = [[], {}, {"status": "response_received", "result": []},
                     {"status": [], "result": {}},
                     {"status": "response_received", "result": {"response": {}}}]
        for value in malformed:
            with self.subTest(evidence=value):
                journal_path.write_text(json.dumps(value), encoding="utf-8")
                result = jobs.status(run, transport=fake)
                self.assertEqual(result["outcome"], "succeeded")
                self.assertEqual(result["diagnostics"][0]["path"], str(journal_path))
                self.assertIn("ValueError", result["diagnostics"][0]["reason"])
        (run / "job-receipt.json").write_text("{}", encoding="utf-8")
        result = jobs.status(run, transport=fake)
        self.assertEqual(result["outcome"], "no_job")
        self.assertEqual(len(result["diagnostics"]), 2)
        self.assertEqual(sum(call[0] == "submit" for call in fake.calls), 1)

    def test_conflicting_receipt_id_reports_diagnostic_and_keeps_journal_id(self):
        fake = FakeTransport()
        self.start(fake)
        run = self.work / "training"
        receipt_path = run / "job-receipt.json"
        receipt = read_json(receipt_path)
        receipt["response"]["id"] = "ftjob-different"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        result = jobs.status(run, transport=fake)
        self.assertEqual(result["outcome"], "succeeded")
        self.assertEqual(result["diagnostics"][0]["path"], str(receipt_path))
        self.assertIn("conflicts", result["diagnostics"][0]["reason"])
        self.assertEqual(fake.calls[-1], ("GET", "job", "ftjob-test"))

    def test_lost_upload_receipt_status_uses_known_file_only(self):
        fake = FakeTransport()
        def save(path, value):
            if Path(path).name == "train-upload-receipt.json":
                Path(path).write_text("{", encoding="utf-8")
                raise OSError("simulated upload receipt failure")
            return write_json(path, value)
        with patch.object(jobs, "write_json", side_effect=save):
            with self.assertRaises(OSError):
                self.start(fake)
        result = jobs.status(self.work / "training", transport=fake)
        self.assertEqual(result["outcome"], "no_job")
        self.assertEqual(set(result["states"]), {"train"})
        self.assertEqual([call[0] for call in fake.calls], ["upload", "GET"])

    def test_status_failure_and_timeout_are_not_success(self):
        fake = FakeTransport(job_state="failed")
        self.start(fake)
        run = self.work / "training"
        self.assertEqual(jobs.status(run, wait=True, transport=fake)["outcome"], "failed")
        fake.job_state = "running"
        clock = Clock()
        result = jobs.status(run, wait=True, transport=fake, clock=clock,
                             sleep=clock.sleep, timeout_seconds=3, poll_seconds=2)
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(clock.now, 3)

    def test_unknown_or_missing_job_status_fails_without_retry(self):
        fake = FakeTransport()
        self.start(fake)
        run = self.work / "training"
        for state in (None, "", "unrecognized", [], {}):
            fake.job_state = state
            before = len(fake.calls)
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, "Unrecognized job status"):
                jobs.status(run, transport=fake)
            self.assertEqual(len(fake.calls), before + 1)
        with patch.object(fake, "status", return_value={"id": "ftjob-test"}) as observe:
            with self.assertRaisesRegex(ValueError, "Unrecognized job status"):
                jobs.status(run, transport=fake)
            observe.assert_called_once()
        for state in ("pending", "validating_files", "queued", "running", "cancelling"):
            fake.job_state = state
            self.assertEqual(jobs.status(run, transport=fake)["outcome"], "pending")
        self.assertEqual(sum(call[0] == "submit" for call in fake.calls), 1)

    def test_unknown_file_status_stops_before_submit_and_status_does_not_retry(self):
        fake = FakeTransport(file_states={"file-train": ["unrecognized"]})
        with self.assertRaisesRegex(ValueError, "Unrecognized file status"):
            self.start(fake)
        self.assertEqual([call[0] for call in fake.calls], ["upload", "upload", "GET"])
        with self.assertRaisesRegex(ValueError, "Unrecognized file status"):
            jobs.status(self.work / "training", transport=fake)
        self.assertEqual([call[0] for call in fake.calls], ["upload", "upload", "GET", "GET"])
        with patch.object(fake, "status", return_value={"id": "file-train"}) as observe:
            with self.assertRaisesRegex(ValueError, "Unrecognized file status"):
                jobs.status(self.work / "training", transport=fake)
            observe.assert_called_once()
        self.assertTrue(list((self.work / "training" / "observations").glob("*.json")))

    def test_invalid_inputs_do_not_load_sdk(self):
        train, val, config = self.inputs()
        train.write_bytes(b'{"messages":[],"oracle":"not allowed"}')
        with patch.object(jobs, "sdk_client", side_effect=AssertionError("SDK must stay unloaded")):
            with self.assertRaises(ValueError):
                jobs.start(train, val, config, self.work / "invalid")
        self.assertFalse((self.work / "invalid").exists())

    def test_poll_options_must_be_positive_finite_before_send(self):
        train, val, config = self.inputs()
        fake = FakeTransport()
        for invalid in (0, -1, float("nan"), float("inf"), True):
            for option in ("poll_seconds", "timeout_seconds"):
                with self.subTest(option=option, value=invalid), self.assertRaises(ValueError):
                    jobs.start(train, val, config, self.work / "invalid",
                               transport=fake, **{option: invalid})
        self.assertFalse(fake.calls)

    def test_training_validation_and_overlap(self):
        train, val, config_path = self.inputs()
        val.write_bytes(train.read_bytes())
        with self.assertRaisesRegex(ValueError, "overlap"):
            jobs.prepare(train, val, config_path, self.work / "invalid")
        config = read_json(config_path)
        for hyperparameters in ({"n_epochs": 0}, {"batch_size": True},
                                {"learning_rate_multiplier": float("inf")}, {"unknown": 1}):
            with self.subTest(hyperparameters=hyperparameters), self.assertRaises(ValueError):
                jobs.training_config({**config, "hyperparameters": hyperparameters})
        train.write_text('{"messages":[{"role":"user","content":"x"},'
                         '{"role":"assistant","content":"y"}]}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "at least 10"):
            jobs.prepare(train, val, config_path, self.work / "invalid")

    def test_size_limit_checked_before_upload(self):
        train, val, config = self.inputs()
        fake = FakeTransport()
        with patch.object(jobs, "MAX_UPLOAD_BYTES", 1), self.assertRaisesRegex(ValueError, "512 MB"):
            jobs.start(train, val, config, self.work / "oversize", transport=fake)
        self.assertFalse(fake.calls)
        self.assertFalse((self.work / "oversize").exists())

    def test_execute_once_writes_before_send_and_never_replays(self):
        path = self.prepare()
        sent = []
        def send():
            self.assertTrue(list((self.work / "attempts").glob("*.started.json")))
            sent.append(True)
            return {"id": "known-response"}
        options = dict(operation="test", input_path=path, target_id="test",
                       run_dir=self.work, payload={"input": 1}, send=send)
        execute_once(**options)
        with self.assertRaises(FileExistsError):
            execute_once(**options)
        self.assertEqual(sent, [True])

    def test_cli_status_exit_codes_and_visible_timeout(self):
        spec = importlib.util.spec_from_file_location("train_cli", ROOT / "scripts" / "train.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        for outcome in ("succeeded", "pending", "failed", "cancelled", "timeout", "no_job"):
            with self.subTest(outcome=outcome), patch.object(
                    jobs, "status", return_value={"outcome": outcome}), patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(cli.main(["status", "--run-dir", str(self.work)]),
                                 0 if outcome in {"succeeded", "pending"} else 1)
        with patch.object(jobs, "status", side_effect=TimeoutError("deadline exceeded")), patch(
                "sys.stderr", new=io.StringIO()) as stderr:
            self.assertEqual(cli.main(["status", "--run-dir", str(self.work), "--wait"]), 1)
            self.assertEqual(json.loads(stderr.getvalue())["outcome"], "timeout")

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
                "model": "test", "training_type": "Standard", "hyperparameters": {"batch_size": "auto"}})


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
        spec = importlib.util.spec_from_file_location("hosted_capture",
                                                     ROOT / "deploy" / "hosted-agent" / "capture.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        endpoint = "https://example.services.ai.azure.com/api/projects/lab"
        plan = {"config": {"project_endpoint": endpoint, "model": "teacher",
                          "max_output_tokens": 10, "max_model_calls": 5, "max_tool_calls": 5}}
        source = self.work / "plan.json"
        write_json(source, plan)
        selected = {"conversation_id": "one", "category": "返品"}
        capture = module.Capture(plan, selected, self.work)

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
        restarted = module.Capture(plan, selected, self.work)
        with self.assertRaises(FileExistsError):
            asyncio.run(restarted.request(request))

    def test_hosted_evidence_contains_observed_tools_and_no_invented_state(self):
        spec = importlib.util.spec_from_file_location("evidence_capture",
                                                     ROOT / "deploy" / "hosted-agent" / "capture.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        endpoint = "https://example.services.ai.azure.com/api/projects/lab"
        plan = {"target": endpoint + "|teacher", "config": {
            "project_endpoint": endpoint, "model": "teacher",
            "max_output_tokens": 10, "max_model_calls": 5, "max_tool_calls": 5}}
        selected = {"conversation_id": "one", "category": "返品",
                    "input_sha256": hashlib.sha256("返品".encode()).hexdigest(), "prompt": "返品"}
        source = self.work / "plan.json"
        write_json(source, plan)
        capture = module.Capture(plan, selected, self.work,
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
        plan = {"target": "test", "config": {"max_model_calls": 5, "max_tool_calls": 5}}
        capture = module.Capture(plan, {"conversation_id": "one", "category": "返品",
            "input_sha256": "0" * 64}, self.work)
        capture.provider_calls = [{"call_id": identifier, "name": "get_order_details",
                                   "arguments": '{"order_id":"ORD-001"}'} for identifier in ("one", "two")]
        with self.assertRaises(RuntimeError):
            capture.call_tool("get_order_details", {"order_id": "ORD-001"},
                              lambda: self.fail("ambiguous proposal must not execute"))
        self.assertIsNone(capture.events[0]["call_id"])
        self.assertEqual(capture.events[0]["event"], "tool_blocked")
        self.assertIsNone(capture.finish("failed_or_unknown")["final_state"])

        capture = module.Capture(plan, {"conversation_id": "two", "category": "返品",
            "input_sha256": "0" * 64}, self.work)
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

        class Fake:
            calls = 0

            def invoke(self, plan):
                self.calls += 1
                return {"http_status": 200, "response": {"status": "completed", "id": "resp-one"},
                        "capture_retrieval": "not_verified"}

        fake = Fake()
        receipt = invocation.invoke(path, run_dir=self.work, transport=fake)
        self.assertFalse(receipt["trace_capture_verified"])
        with self.assertRaises(FileExistsError):
            invocation.invoke(path, run_dir=self.work, transport=fake)
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
