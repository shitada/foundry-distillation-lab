"""Bounded LOCAL inference/tool loop; not a Hosted Agent observation."""

from copy import deepcopy
import hashlib
import time

from .schema import ContractError, call_errors, canonical, parse_call, tool_schemas
from .scoring import aggregate_usage, derive_final_state, normalized_usage


class RecordedModel:
    """Write-ahead, no-retry boundary for a direct model call.

    The caller supplies a durable Journal. Existing start records
    must be rejected by Journal.start, including starts with no finish record.
    """

    def __init__(self, invoke, *, journal, target):
        if not isinstance(target, str) or not target.strip():
            raise ValueError("explicit_model_target_required")
        self.invoke = invoke
        self.journal = journal
        self.target = target

    def __call__(self, payload):
        attempt_id = payload["attempt_id"]
        request = deepcopy(payload)
        request["target"] = self.target
        self.journal.start(attempt_id, request)
        # No retry, even on a timeout or a failure to persist the response.
        try:
            response = self.invoke(request)
            canonical(response)
        except Exception as exc:
            self.journal.finish(attempt_id, "unknown", {"error_type": type(exc).__name__})
            raise
        self.journal.finish(attempt_id, "received", {"response": response})
        return response


def _attempt_id(model, case_id, index):
    digest = hashlib.sha256(canonical([model, case_id]).encode("utf-8")).hexdigest()[:24]
    return f"eval-{digest}-{index:04d}"


def _message(response):
    if isinstance(response, dict) and response.get("response_error"):
        raise ValueError(response["response_error"])
    if not isinstance(response, dict) or not isinstance(response.get("message"), dict):
        raise ValueError("model_response_requires_message")
    canonical(response)
    message = response["message"]
    calls = message.get("tool_calls", [])
    if not isinstance(calls, list):
        raise ValueError("tool_calls_not_list")
    if message.get("content") is not None and not isinstance(message["content"], str):
        raise ValueError("content_not_string")
    return message, calls


def _response_identity(response):
    return {key: deepcopy(response[key]) for key in ("response_model", "response_id")
            if isinstance(response, dict) and key in response}


def run_next_action(case, model, invoke, tools):
    """One injected request, no tool execution and no retry."""
    tool_schemas(tools)
    if not isinstance(case.get("messages"), list) or not case["messages"]:
        raise ContractError("next_action_messages_required")
    started = time.monotonic()
    record = {"case_id": case["case_id"], "model": model, "status": "unknown",
              "origin": "local_next_action", "message": None, "usage": normalized_usage(None)}
    try:
        response = invoke({"attempt_id": _attempt_id(model, case["case_id"], 1),
                           "model_label": model, "messages": deepcopy(case["messages"]),
                           "tools": deepcopy(tools)})
        record.update(_response_identity(response))
        message, _ = _message(response)
        record.update(status="completed", message=deepcopy(message),
                      usage=normalized_usage(response.get("usage")))
    except Exception as exc:
        record["error_type"] = type(exc).__name__
    record["latency_seconds"] = time.monotonic() - started
    return record


def run_case(case, model, invoke, session_factory, *, max_model_calls=12, max_tool_calls=24,
             tools=None):
    """Each invocation constructs a fresh RetailSession; no cross-case replay.

    invoke(payload) -> {"message": OpenAI-style message, "usage": normalized dict}.
    session_factory() -> object with .call(name, arguments), .tools, .system_prompt.
    A raised tool exception has unknown side effects: stop, never blindly retry.
    """
    if any(type(value) is not int or value <= 0 for value in (max_model_calls, max_tool_calls)):
        raise ValueError("positive_integer_loop_limits_required")
    if not isinstance(case.get("user_input"), str) or not case["user_input"].strip():
        raise ContractError("case_user_input_required")
    session = session_factory()
    schemas = tool_schemas(session.tools)
    if tools is not None and canonical(session.tools) != canonical(tools):
        raise ContractError("session_tools_differ_from_input")
    if not isinstance(session.system_prompt, str) or not session.system_prompt:
        raise ContractError("session_system_prompt_required")
    if "system_prompt" in case and case["system_prompt"] != session.system_prompt:
        raise ContractError("session_prompt_differs_from_input")
    messages = [{"role": "system", "content": session.system_prompt},
                {"role": "user", "content": case["user_input"]}]
    events, usages, successful, seen_ids = [], [], [], set()
    record = {"case_id": case["case_id"], "model": model, "origin": "local_tool_loop",
              "status": "unknown", "answer": "", "events": events}
    started, tool_count = time.monotonic(), 0
    try:
        for index in range(1, max_model_calls + 1):
            model_id = f"model-{index}"
            events.append({"event": "model_start", "call_id": model_id})
            response = None
            try:
                response = invoke({"attempt_id": _attempt_id(model, case["case_id"], index),
                                   "model_label": model, "messages": deepcopy(messages),
                                   "tools": deepcopy(session.tools)})
                record.update(_response_identity(response))
                message, calls = _message(response)
            except Exception as exc:
                usages.append(None)
                events.append({"event": "model_finish", "call_id": model_id, "status": "unknown",
                               "error_type": type(exc).__name__, **_response_identity(response)})
                record["error_type"] = type(exc).__name__
                break
            usages.append(response.get("usage"))
            events.append({"event": "model_finish", "call_id": model_id, "status": "completed",
                           "message": deepcopy(message), "usage": normalized_usage(response.get("usage")),
                           **_response_identity(response)})
            if not calls:
                record["answer"] = message.get("content") or ""
                record["status"] = "completed" if record["answer"].strip() else "error"
                break
            # Validate the entire batch before executing any member.
            parsed_calls, invalid = [], []
            batch_ids = set()
            for position, call in enumerate(calls):
                call_id = call.get("id") if isinstance(call, dict) else None
                if not isinstance(call_id, str) or not call_id or call_id in seen_ids | batch_ids:
                    invalid.append({"event": "tool_blocked", "call_id": f"invalid-{index}-{position}",
                                    "reason": "missing_or_reused_call_id"})
                    continue
                batch_ids.add(call_id)
                try:
                    parsed = parse_call(call)
                    errors = call_errors(parsed, schemas)
                    if errors:
                        raise ValueError(";".join(errors))
                    parsed_calls.append((call_id, parsed))
                except (ValueError, TypeError):
                    invalid.append({"event": "tool_blocked", "call_id": call_id,
                                    "reason": "invalid_tool_name_or_arguments"})
            if invalid:
                events.extend(invalid)
                record["status"] = "blocked"
                break
            if tool_count + len(parsed_calls) > max_tool_calls:
                record["status"] = "limit_exceeded"
                break
            seen_ids.update(batch_ids)
            messages.append({"role": "assistant", "content": message.get("content"),
                             "tool_calls": [
                                 {"id": call_id, "type": "function",
                                  "function": {"name": parsed["name"], "arguments": canonical(parsed["arguments"])}}
                                 for call_id, parsed in parsed_calls]})
            stopped = False
            for call_id, parsed in parsed_calls:
                tool_count += 1
                events.append({"event": "tool_start", "call_id": call_id, **deepcopy(parsed)})
                try:
                    result = session.call(parsed["name"], deepcopy(parsed["arguments"]))
                    canonical(result)
                    if not isinstance(result, dict):
                        raise ValueError("tool_result_not_object")
                except Exception as exc:
                    events.append({"event": "tool_finish", "call_id": call_id, "status": "unknown",
                                   "error_type": type(exc).__name__})
                    record["status"], stopped = "unknown", True
                    break
                result_status = result.get("status")
                if result.get("error") or result_status in ("error", "failed", "blocked", "rejected"):
                    # A structured rejection is only "blocked" if the tool explicitly
                    # attests no external side effect. Otherwise outcome is unknown.
                    status = "blocked" if result.get("external_side_effect") is False else "unknown"
                    events.append({"event": "tool_finish", "call_id": call_id, "status": status,
                                   "result": deepcopy(result)})
                    record["status"], stopped = status, True
                    break
                events.append({"event": "tool_finish", "call_id": call_id, "status": "completed",
                               "result": deepcopy(result)})
                successful.append({**parsed, "result": deepcopy(result)})
                messages.append({"role": "tool", "tool_call_id": call_id, "content": canonical(result)})
            if stopped:
                break
        else:
            record["status"] = "limit_exceeded"
    finally:
        events.append({"event": "attempt_finish", "status": record["status"]})
        record["latency_seconds"] = time.monotonic() - started
        record["usage"] = aggregate_usage(usages)
        record["final_state"] = derive_final_state(successful)
        record["model_calls"] = sum(event["event"] == "model_start" for event in events)
        record["tool_calls"] = tool_count
    return record


class OpenAITransport:
    """Optional direct model transport. Construction/import makes no cloud call."""

    def __init__(self, *, base_url, max_completion_tokens=2048, timeout_seconds=60):
        if not isinstance(base_url, str) or not base_url.startswith("https://") or not base_url.endswith("/openai/v1/"):
            raise ValueError("https_openai_v1_endpoint_required")
        if type(max_completion_tokens) is not int or max_completion_tokens <= 0:
            raise ValueError("positive_max_completion_tokens_required")
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 600:
            raise ValueError("timeout_must_be_between_zero_and_600_seconds")
        self.base_url = base_url
        self.max_completion_tokens = max_completion_tokens
        self.timeout_seconds = timeout_seconds
        self._client = None

    def __call__(self, payload):
        if self._client is None:
            # Deliberately lazy: offline scoring never imports SDKs or credentials.
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
            from openai import OpenAI
            token = get_bearer_token_provider(DefaultAzureCredential(),
                                             "https://cognitiveservices.azure.com/.default")
            self._client = OpenAI(base_url=self.base_url, api_key=token, max_retries=0,
                                  timeout=self.timeout_seconds)
        options = ({"response_format": payload["response_format"]}
                   if "response_format" in payload else
                   {"tools": payload["tools"], "parallel_tool_calls": False})
        response = self._client.chat.completions.create(
            model=payload["target"], messages=payload["messages"],
            max_completion_tokens=self.max_completion_tokens, store=False, **options)
        raw = response.model_dump()
        identity = {f"response_{key}": raw[key] for key in ("model", "id") if key in raw}
        usage = raw.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        measured_usage = {"input_tokens": usage.get("prompt_tokens"),
                          "output_tokens": usage.get("completion_tokens"),
                          "cached_input_tokens": details.get("cached_tokens")}
        choices = raw.get("choices", [])
        if (not isinstance(choices, list) or len(choices) != 1
                or not isinstance(choices[0], dict)
                or choices[0].get("finish_reason") not in {"stop", "tool_calls"}):
            return {**identity, "usage": measured_usage,
                    "response_error": "incomplete_or_ambiguous_model_response",
                    "choices": choices}
        return {"message": choices[0].get("message"),
                **identity, "finish_reason": choices[0]["finish_reason"], "usage": measured_usage}
