"""Offline adapter for observed Hosted runtime evidence, never model-history replay."""

from copy import deepcopy
import hashlib

from .schema import ContractError, canonical
from .scoring import DEFAULT_MODELS, evaluate


def _native_message(output):
    if not isinstance(output, list):
        return None
    parts, calls = [], []
    for item in output:
        if not isinstance(item, dict):
            return None
        kind = item.get("type")
        if kind == "reasoning":
            continue
        if kind == "function_call":
            calls.append({"id": item.get("call_id"), "type": "function",
                          "function": {"name": item.get("name"), "arguments": item.get("arguments")}})
        elif kind == "message" and item.get("role") == "assistant":
            content = item.get("content")
            if not isinstance(content, list):
                return None
            for part in content:
                if (not isinstance(part, dict) or part.get("type") != "output_text"
                        or not isinstance(part.get("text"), str)):
                    return None
                parts.append(part["text"])
        else:
            return None
    return {"content": "".join(parts) if parts else None, "tool_calls": calls} if parts or calls else None


def _native_usage(usage):
    if not isinstance(usage, dict):
        return None
    if "cached_input_tokens" in usage:
        return {key: usage.get(key) for key in ("input_tokens", "output_tokens", "cached_input_tokens")}
    details = usage.get("input_tokens_details")
    return {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "cached_input_tokens": details.get("cached_tokens") if isinstance(details, dict) else None}


def _native_capture(capture):
    """Normalize only fields actually present in the instrumented native log."""
    identity, source = capture.get("runtime_identity"), capture.get("source")
    if not isinstance(identity, dict) or not isinstance(source, dict):
        raise ContractError("native_hosted_runtime_identity_and_source_required")
    model, endpoint = identity.get("model"), identity.get("endpoint")
    if not model or not endpoint:
        target = source.get("target")
        if not isinstance(target, str) or target.count("|") != 1:
            raise ContractError("native_hosted_target_identity_required")
        target_endpoint, target_model = target.split("|")
        model, endpoint = model or target_model, endpoint or target_endpoint
    if isinstance(source.get("target"), str) and source["target"] != f"{endpoint}|{model}":
        raise ContractError("native_hosted_target_identity_conflict")
    status = "completed" if capture.get("status") in ("completed_unreviewed", "completed") else "unknown"
    events = capture.get("events")
    normalized = [] if isinstance(events, list) else None
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            normalized.append(deepcopy(event))
            continue
        result = deepcopy(event)
        kind = event.get("event")
        if kind in ("model_start", "model_finish"):
            index = event.get("model_call_index")
            result["call_id"] = f"model-{index}" if type(index) is int and index > 0 else None
            if kind == "model_finish":
                result["message"] = _native_message(event.get("output"))
                result["usage"] = _native_usage(event.get("usage"))
                result["status"] = "completed" if event.get("status") == "completed" else "unknown"
        elif kind in ("tool_start", "tool_finish"):
            # Diagnostic proposal matching is not authoritative framework identity.
            result["call_id"] = event.get("call_id")
            if (event.get("framework_call_id_verified") is False
                    or event.get("framework_context_call_id_available") is False):
                result["reported_call_id"] = event.get("call_id")
                result["call_id"] = None
            if kind == "tool_finish":
                returned = event.get("result")
                result["status"] = "completed" if event.get("status") in ("returned", "completed") else "unknown"
                if isinstance(returned, dict) and (
                        returned.get("error") or returned.get("status") in ("error", "failed", "blocked", "rejected")):
                    result["status"] = "blocked" if returned.get("external_side_effect") is False else "unknown"
        elif kind == "attempt_finish":
            result["status"] = "completed" if event.get("status") in ("completed_unreviewed", "completed") else "unknown"
        normalized.append(result)
    final_state = capture.get("final_state")
    if capture.get("final_state_available") is not True:
        if final_state is not None:
            raise ContractError("native_final_state_availability_inconsistent")
        final_state = None
    return {
        **deepcopy(capture), "kind": "hosted-retail-evidence",
        "agent": {"name": identity.get("framework_agent_name"), "id": None, "version": None},
        "runtime": {
            "kind": "foundry_hosted_agent", "model": model, "endpoint": endpoint,
            "native_identity": deepcopy(identity),
            "model_identity_basis": "recorded_collection_target_not_resolved_model_version",
            "latency_scope": "native_conversation_elapsed_seconds_including_buffered_model_http",
        },
        "status": status, "events": normalized, "answer": capture.get("final_answer"),
        "final_state": deepcopy(final_state), "usage": None,
        "latency_seconds": capture.get("elapsed_seconds"),
    }


def import_hosted(bundle, captures, model_label, source_sha256):
    """Import independently captured execution events into declared E2E cases.

    A missing event, final state, usage, or answer stays missing. Raw Responses
    request/response captures cannot prove that a proposed tool really executed.
    """
    evaluate(bundle, "e2e")
    models = bundle.get("models", list(DEFAULT_MODELS))
    if model_label not in models:
        raise ContractError("hosted_model_label_not_in_evaluation")
    targets = bundle.get("hosted_targets", {})
    if not isinstance(targets, dict):
        raise ContractError("hosted_targets_must_be_object")
    target = targets.get(model_label)
    if (not isinstance(target, dict)
            or any(not isinstance(target.get(key), str) or not target[key]
                   for key in ("agent_name", "model", "endpoint"))):
        raise ContractError("hosted_target_requires_agent_name_model_endpoint")
    if not isinstance(captures, list) or not captures:
        raise ContractError("nonempty_hosted_capture_list_required")
    cases = {}
    for case in bundle["cases"]:
        identity = case.get("conversation_id", case["case_id"])
        if not isinstance(identity, str) or not identity or identity in cases:
            raise ContractError("unique_case_conversation_identity_required")
        if not isinstance(case.get("user_input"), str) or not case["user_input"]:
            raise ContractError("hosted_case_requires_user_input")
        cases[identity] = case
    output = deepcopy(bundle)
    records = output.setdefault("records", [])
    existing = {(record["case_id"], record["model"]) for record in records}
    for index, capture in enumerate(captures, 1):
        original = deepcopy(capture)
        if isinstance(capture, dict) and capture.get("kind") == "foundry-responses-capture":
            raise ContractError(
                "model_http_capture_is_insufficient: require hosted-retail-evidence "
                "with observed tool execution events, input/agent identity and final state")
        if isinstance(capture, dict) and capture.get("kind") == "hosted-conversation-evidence":
            capture = _native_capture(capture)
        if (not isinstance(capture, dict) or capture.get("kind") != "hosted-retail-evidence"
                or type(capture.get("schema_version")) is not int or capture["schema_version"] != 1):
            raise ContractError("unsupported_hosted_capture_schema")
        conversation_id = capture.get("conversation_id")
        if not isinstance(conversation_id, str) or conversation_id not in cases:
            raise ContractError("hosted_conversation_not_in_declared_cases")
        case = cases[conversation_id]
        key = (case["case_id"], model_label)
        if key in existing:
            raise ContractError("duplicate_hosted_case_model_evidence")
        expected_hash = hashlib.sha256(case["user_input"].encode("utf-8")).hexdigest()
        if capture.get("user_input") != case["user_input"] or capture.get("input_sha256") != expected_hash:
            raise ContractError("hosted_input_identity_mismatch")
        agent, runtime = capture.get("agent"), capture.get("runtime")
        if not isinstance(agent, dict) or not isinstance(runtime, dict):
            raise ContractError("hosted_agent_and_runtime_identity_required")
        if (agent.get("name") != target["agent_name"]
                or runtime.get("kind") != "foundry_hosted_agent"
                or runtime.get("model") != target["model"]
                or runtime.get("endpoint") != target["endpoint"]):
            raise ContractError("hosted_agent_or_runtime_identity_mismatch")
        for expected_key, captured_key in (("agent_id", "id"), ("agent_version", "version")):
            if expected_key in target and (
                    not isinstance(target[expected_key], str) or not target[expected_key]
                    or agent.get(captured_key) != target[expected_key]):
                raise ContractError("hosted_agent_version_or_resource_mismatch")
        if canonical(capture.get("tools")) != canonical(case.get("tools", bundle.get("tools"))):
            raise ContractError("hosted_tool_contract_mismatch")
        record = {
            "case_id": case["case_id"], "model": model_label,
            "status": capture.get("status", "unknown"),
            "origin": "hosted_capture_import",
            "events": deepcopy(capture.get("events")),
            "answer": capture.get("answer"),
            "final_state": deepcopy(capture.get("final_state")),
            "usage": deepcopy(capture.get("usage")),
            "latency_seconds": capture.get("latency_seconds"),
            "provenance": {
                "runtime": deepcopy(runtime), "agent": deepcopy(agent),
                "capture_source": deepcopy(capture.get("source")),
                "conversation_id": conversation_id, "input_sha256": expected_hash,
                "source_sha256": source_sha256, "capture_index": index,
                "capture_sha256": hashlib.sha256(canonical(original).encode("utf-8")).hexdigest(),
                "capture_format": original["kind"],
                "capture_files": deepcopy(original.get("capture_files")),
                "native_known_usage": deepcopy(original.get("known_usage")),
                "observed_status": original.get("observed_status", original.get("status")),
                "final_state_scope": original.get("final_state_scope"),
                "tool_call_linkage": original.get("tool_call_linkage"),
                "evidence_kind": capture.get("evidence_kind", "unverified_hosted_capture"),
                "identity_assurance": "captured_metadata_not_remote_attestation",
            },
        }
        # Import changes the record envelope. Never silently rebind a review hash.
        if "review" in capture:
            record["source_review"] = deepcopy(capture["review"])
        records.append(record)
        existing.add(key)
    imports = output.setdefault("imports", [])
    if not isinstance(imports, list):
        raise ContractError("imports_metadata_must_be_list")
    imports.append({"format": "hosted-execution-evidence-v1",
                    "capture_formats": sorted({capture["kind"] for capture in captures}),
                    "source_sha256": source_sha256,
                    "model_label": model_label, "records": len(captures),
                    "network_calls": 0, "reviews_rebound": False})
    # Incomplete captures produce technical failures; this never creates evidence.
    evaluate(output, "e2e")
    return output
