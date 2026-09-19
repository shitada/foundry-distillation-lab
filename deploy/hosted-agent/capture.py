"""HTTP response capture; no credentials/headers are written to artifacts."""

import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone
import time

from foundry_distillation_lab.io import canonical, parse_json, sha256, write_json, write_jsonl
from foundry_distillation_lab.safety import Journal


def terminal_response(raw):
    text = raw.decode("utf-8")
    if text.lstrip().startswith("{"):
        return json.loads(text)
    terminal = None
    for line in text.splitlines():
        if line.startswith("data: ") and line[6:] != "[DONE]":
            event = json.loads(line[6:])
            if event.get("type") in {"response.completed", "response.failed", "response.incomplete"}:
                terminal = event["response"]
    if terminal is None:
        raise ValueError("No terminal Responses event; outcome remains unknown")
    return terminal


def normalized_usage(usage):
    if usage is None:
        return None
    return {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "cached_input_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens")}


def assistant_message(response):
    if response.get("status") != "completed":
        return None
    message = {"role": "assistant", "content": None, "tool_calls": []}
    calls, text = [], []
    for item in response.get("output", []):
        if item.get("type") == "function_call":
            calls.append({"id": item["call_id"], "type": "function",
                          "function": {"name": item["name"], "arguments": item["arguments"]}})
        elif item.get("type") == "message" and item.get("role") == "assistant":
            text.extend(part["text"] for part in item.get("content", [])
                        if part.get("type") == "output_text")
    if text:
        message["content"] = "".join(text)
    if calls:
        message["tool_calls"] = calls
    return message if text or calls else None


class Capture:
    def __init__(self, plan, approved_input, approval, run_dir, runtime_identity=None):
        self.plan, self.input, self.approval = plan, approved_input, approval
        self.run_dir = Path(run_dir)
        self.journal = Journal(self.run_dir)
        self.counter = 0
        self.pending = {}
        self.prefix = hashlib.sha256(approved_input["conversation_id"].encode()).hexdigest()[:24]
        self.started = time.monotonic()
        self.events = []
        self.captures = []
        self.usage = []
        self.provider_calls = []
        self.seen_provider_ids = set()
        self.tool_counter = 0
        self.tool_failure = False
        self.final_answer = None
        self.runtime_identity = runtime_identity
        self.submissions = []
        self.tools = None
        self.system_prompt_sha256 = None

    def set_contract(self, tools, system_prompt=None):
        self.tools = parse_json(canonical(tools))
        self.system_prompt_sha256 = (hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
                                     if isinstance(system_prompt, str) else None)

    @property
    def source(self):
        return {"target": self.plan["target"],
                "collection_plan_sha256": self.approval.data["input_sha256"]}

    def event(self, kind, **values):
        record = {"event": kind, "conversation_id": self.input["conversation_id"],
                  "sequence": len(self.events) + 1,
                  "at_utc": datetime.now(timezone.utc).isoformat(),
                  "elapsed_seconds": time.monotonic() - self.started, **values}
        record = parse_json(canonical(record))
        write_json(self.run_dir / f"{self.prefix}-{record['sequence']:05d}.event.json", record)
        self.events.append(record)
        return record

    def call_tool(self, name, arguments, execute):
        if self.tool_failure:
            raise RuntimeError("A prior tool failed with unknown side effects; no further tool execution")
        matches = [call for call in self.provider_calls if call["name"] == name
                   and canonical(parse_json(call["arguments"])) == canonical(arguments)]
        if len(matches) != 1:
            self.tool_failure = True
            self.event("tool_blocked", call_id=None, name=name, arguments=arguments, status="unknown",
                       reason="observed_proposal_missing_or_ambiguous")
            raise RuntimeError("Tool proposal correlation is not unique; execution blocked")
        provider_id = matches[0]["call_id"]
        self.provider_calls.remove(matches[0])
        self.tool_counter += 1
        tool_id = f"tool-{self.prefix}-{self.tool_counter}"
        identity = {"tool_event_id": tool_id, "provider_call_id": provider_id,
                    "call_id": provider_id, "name": name, "arguments": parse_json(canonical(arguments)),
                    "call_id_source": "unique_unconsumed_observed_proposal",
                    "framework_context_call_id_available": False}
        self.event("tool_start", **identity)
        try:
            result = execute()
        except BaseException as exc:
            self.tool_failure = True
            self.event("tool_finish", **identity, status="unknown", observed_status="exception_unknown",
                       result=None, error_type=type(exc).__name__)
            raise
        try:
            self.event("tool_finish", **identity, status="completed", observed_status="returned", result=result)
        except BaseException:
            self.tool_failure = True
            raise
        if (name == "submit_resolution" and isinstance(result, dict)
                and result.get("status") == "処理シミュレーション完了"
                and result.get("external_side_effect") is False):
            self.submissions.append(parse_json(canonical(result)))
        return result

    async def request(self, request):
        if self.tool_failure:
            raise RuntimeError("A tool failed; block subsequent model requests rather than replay")
        if self.provider_calls:
            self.tool_failure = True
            raise RuntimeError("Unconsumed tool proposal; block subsequent model request")
        config = self.plan["config"]
        expected = config["project_endpoint"].rstrip("/") + "/openai/v1/responses"
        if request.method != "POST" or str(request.url).split("?")[0] != expected:
            raise ValueError("Unapproved model request route")
        body = json.loads(await request.aread())
        if body.get("model") != config["model"] or body.get("store") is not False:
            raise ValueError("Model target/store setting changed")
        if body.get("previous_response_id"):
            raise ValueError("Remote continuation is not supported")
        if body.get("max_output_tokens") != config["max_output_tokens"]:
            raise ValueError("Output token cap changed")
        self.counter += 1
        self.final_answer = None
        attempt_id = f"collect-{self.prefix}-{self.counter}"
        self.journal.start(attempt_id, body)
        self.approval.reserve(config["estimated_cost_per_model_request"])
        self.pending[id(request)] = (attempt_id, body, self.counter)
        self.event("model_start", model_call_index=self.counter,
                   call_id=f"model-{self.counter}", request=body)

    async def response(self, response):
        attempt_id, request, index = self.pending[id(response.request)]
        finished_event = False
        try:
            result = terminal_response(await response.aread())
            capture = {"kind": "foundry-responses-capture",
                       "conversation_id": self.input["conversation_id"],
                       "category": self.input["category"], "call_index": index,
                       "source": self.source,
                       "request": request, "response": result}
            path = self.run_dir / f"{self.prefix}-{index}.capture.jsonl"
            write_jsonl(path, [capture])
            self.captures.append({"path": path.name, "sha256": sha256(path)})
            self.usage.append(result.get("usage"))
            self.event("model_finish", model_call_index=index, call_id=f"model-{index}",
                       response_id=result.get("id"), message=assistant_message(result),
                       status=result.get("status"), output=result.get("output"),
                       usage=normalized_usage(result.get("usage")), raw_usage=result.get("usage"))
            finished_event = True
            calls = [item for item in result.get("output", []) if item.get("type") == "function_call"]
            for call in calls:
                identifier = call.get("call_id")
                if (not isinstance(identifier, str) or not identifier
                        or identifier in self.seen_provider_ids):
                    self.tool_failure = True
                    raise ValueError("Missing or repeated provider call ID; execution blocked")
                self.seen_provider_ids.add(identifier)
            self.provider_calls.extend(calls)
            if result.get("status") == "completed" and not any(
                    item.get("type") == "function_call" for item in result.get("output", [])):
                texts = [part["text"] for item in result.get("output", [])
                         if item.get("type") == "message" and item.get("role") == "assistant"
                         for part in item.get("content", []) if part.get("type") == "output_text"]
                self.final_answer = "".join(texts) if texts else None
            self.journal.finish(attempt_id, "response_received",
                                {"response_id": result.get("id"), "known_usage": result.get("usage"),
                                 "actual_cost": None, "unknown_cost": True})
        except BaseException as exc:
            self.journal.finish(attempt_id, "outcome_unknown",
                                {"error_type": type(exc).__name__, "actual_cost": None,
                                 "unknown_cost": True})
            if not finished_event:
                self.usage.append(None)
                self.event("model_finish", model_call_index=index, call_id=f"model-{index}",
                           response_id=None, message=None, status="outcome_unknown", output=None, usage=None)
            raise
        finally:
            self.pending.pop(id(response.request), None)

    def finish_unknown(self):
        for attempt_id, _, index in self.pending.values():
            self.journal.finish(attempt_id, "outcome_unknown",
                                {"actual_cost": None, "unknown_cost": True})
            self.usage.append(None)
            self.event("model_finish", model_call_index=index, call_id=f"model-{index}",
                       response_id=None, message=None, status="outcome_unknown", output=None, usage=None)
        self.pending.clear()

    def finish(self, status):
        if self.tool_failure or self.provider_calls:
            status = "failed_or_unknown"
        normalized_status = "completed" if status == "completed_unreviewed" else "unknown"
        self.event("attempt_finish", status=normalized_status, observed_status=status)
        answer = self.final_answer if status == "completed_unreviewed" else None
        state = None
        if status == "completed_unreviewed" and not self.tool_failure and answer is not None:
            state = {"terminal": "simulation_submitted" if self.submissions else "answer_only",
                     "submissions": self.submissions}
        observed_usage = [normalized_usage(value) for value in self.usage]
        usage = None if not observed_usage else {
            key: sum(value[key] for value in observed_usage)
            if all(value is not None and value[key] is not None for value in observed_usage) else None
            for key in ("input_tokens", "output_tokens", "cached_input_tokens")}
        canonical_events = [event for event in self.events if event["event"] in {
            "model_start", "model_finish", "tool_start", "tool_finish", "tool_blocked", "attempt_finish"}]
        evidence = {"schema_version": 1, "kind": "hosted-retail-evidence",
            "conversation_id": self.input["conversation_id"], "category": self.input["category"],
            "input_sha256": self.input["input_sha256"], "source": self.source,
            "user_input": self.input.get("prompt"),
            "agent": {"name": (self.runtime_identity or {}).get("framework_agent_name"),
                      "id": None, "version": None},
            "runtime": {"kind": "foundry_hosted_agent", "model": self.plan.get("config", {}).get("model"),
                        "endpoint": self.plan.get("config", {}).get("project_endpoint"),
                        "system_prompt_sha256": self.system_prompt_sha256,
                        "tool_schema_sha256": (hashlib.sha256(canonical(self.tools).encode()).hexdigest()
                                               if self.tools is not None else None),
                        "implementation_hashes": (self.runtime_identity or {}).get("retail_hashes"),
                        "runtime_hashes": (self.runtime_identity or {}).get("runtime_hashes"),
                        "sdk_versions": (self.runtime_identity or {}).get("package_versions"),
                        "metadata_source": "local_runtime_files_not_remote_attestation",
                        "latency_scope": "buffered HTTP plus actual tool loop",
                        "metadata": self.runtime_identity,
                        "attempt_start": next((event for event in self.events
                                               if event["event"] == "attempt_start"), None)},
            "tools": self.tools,
            "runtime_identity": self.runtime_identity, "status": normalized_status,
            "observed_status": status, "quality_review": "required",
            "elapsed_seconds": time.monotonic() - self.started, "events": canonical_events,
            "latency_seconds": time.monotonic() - self.started, "usage": usage,
            "capture_files": self.captures,
            "final_answer": answer, "answer": answer,
            "final_state": state, "final_state_available": state is not None,
            "final_state_scope": "observed_tool_returns_not_store_snapshot",
            "tool_call_linkage": "unique_unconsumed_observed_proposal", "known_usage": self.usage,
            "actual_cost": None, "unknown_cost": True}
        write_json(self.run_dir / f"{self.prefix}.evidence.json", evidence)
        return evidence
