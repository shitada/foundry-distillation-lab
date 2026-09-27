"""Evidence for a completed assessment that intentionally stops an unsuccessful case."""

from copy import deepcopy

from .schema import call_errors, equal, parse_call


def inspect_calls(calls, schemas, seen_ids):
    parsed, errors, batch_ids = [], {}, set()
    for position, call in enumerate(calls):
        identifier = call.get("id") if isinstance(call, dict) else None
        if not isinstance(identifier, str) or not identifier or identifier in seen_ids | batch_ids:
            errors[position] = ["missing_or_reused_call_id"]
            continue
        batch_ids.add(identifier)
        try:
            value = parse_call(call)
            violations = call_errors(value, schemas)
        except (ValueError, TypeError):
            errors[position] = ["invalid_tool_arguments"]
            continue
        if violations:
            errors[position] = violations
        else:
            parsed.append((identifier, value))
    return parsed, errors


def block_calls(calls, model_id, *, errors=None, reason, offset=0):
    """Account for every unexecuted proposal, even missing or duplicate provider IDs."""
    errors = errors or {}
    return [{
        "event": "tool_blocked", "call_id": f"blocked-{model_id}-{position + offset}",
        "model_call_id": model_id, "proposal_index": position + offset,
        "proposal": deepcopy(call),
        "reason": "invalid_tool_call" if position + offset in errors else reason,
        "details": deepcopy(errors.get(position + offset, [])),
    } for position, call in enumerate(calls)]


def stopped(record, reason, status):
    record["status"] = status
    record["termination"] = {"kind": "model_failure", "reason": reason}


def safe_rejection(result):
    return (isinstance(result, dict) and result.get("external_side_effect") is False
            and bool(result.get("error") or result.get("status") in
                     ("error", "failed", "blocked", "rejected")))


def failure_evidence(record, schemas):
    """Return a witnessed termination and accounted-for blocks, never trust a status alone.

    General event consistency is still checked by the scorer. This only validates
    why a final answer or an executable proposal is deliberately absent.
    """
    if not isinstance(record, dict) or record.get("origin") != "local_tool_loop":
        return None
    termination = record.get("termination")
    if not isinstance(termination, dict) or set(termination) != {"kind", "reason"}:
        return None
    states = {"invalid_tool_call": "blocked", "tool_call_limit": "limit_exceeded",
              "model_call_limit": "limit_exceeded", "tool_rejected": "blocked",
              "empty_answer": "error"}
    reason = termination["reason"]
    if (termination["kind"] != "model_failure" or not isinstance(reason, str)
            or reason not in states or record.get("status") != states[reason]):
        return None
    events = record.get("events")
    if (not isinstance(events, list) or not events
            or any(not isinstance(event, dict) for event in events)
            or events[-1] != {"event": "attempt_finish", "status": states[reason],
                              "termination": termination}):
        return None
    finishes = [i for i, event in enumerate(events) if event.get("event") == "model_finish"]
    if not finishes:
        return None
    last_index = finishes[-1]
    last = events[last_index]
    message = last.get("message")
    model_id = last.get("call_id")
    if last.get("status") != "completed" or not isinstance(message, dict) or not isinstance(model_id, str):
        return None
    calls = message.get("tool_calls", [])
    if not isinstance(calls, list):
        return None
    tail = events[last_index + 1:-1]
    if reason == "empty_answer":
        content = message.get("content")
        if (calls or tail or (content is not None and not isinstance(content, str))
                or (content or "").strip() or record.get("answer") != (content or "")):
            return None
        return {"reason": reason, "blocks": [], "skipped": set()}
    if not calls or record.get("answer") != "":
        return None
    seen = set()
    for index in finishes[:-1]:
        prior = events[index].get("message")
        if not isinstance(prior, dict) or not isinstance(prior.get("tool_calls", []), list):
            return None
        for call in prior.get("tool_calls", []):
            if isinstance(call, dict) and isinstance(call.get("id"), str):
                seen.add(call["id"])
    _, errors = inspect_calls(calls, schemas, seen)
    blocks = []
    if reason == "invalid_tool_call":
        if not errors:
            return None
        blocks = block_calls(calls, model_id, errors=errors, reason="batch_contains_invalid_call")
        if not equal(tail, blocks):
            return None
    elif reason in ("tool_call_limit", "model_call_limit"):
        limits = record.get("limits")
        if (errors or not isinstance(limits, dict)
                or set(limits) != {"max_model_calls", "max_tool_calls"}
                or any(type(limit) is not int or limit < 1 for limit in limits.values())):
            return None
        model_count = sum(event.get("event") == "model_start" for event in events)
        tool_count = sum(event.get("event") == "tool_start" for event in events)
        if model_count > limits["max_model_calls"] or tool_count > limits["max_tool_calls"]:
            return None
        if reason == "tool_call_limit":
            if tool_count + len(calls) <= limits["max_tool_calls"]:
                return None
            blocks = block_calls(calls, model_id, reason="tool_call_limit")
            if not equal(tail, blocks):
                return None
        elif (model_count != limits["max_model_calls"] or not tail
              or any(event.get("event") not in ("tool_start", "tool_finish") for event in tail)
              or tail[-1].get("event") != "tool_finish"
              or any(event.get("status") != "completed" for event in tail
                     if event.get("event") == "tool_finish")):
            return None
    else:
        if errors:
            return None
        rejected = [i for i, event in enumerate(tail) if event.get("event") == "tool_finish"
                    and event.get("status") == "blocked" and safe_rejection(event.get("result"))]
        if len(rejected) != 1:
            return None
        index = rejected[0]
        identifier = tail[index].get("call_id")
        positions = [i for i, call in enumerate(calls) if call.get("id") == identifier]
        if len(positions) != 1:
            return None
        offset = positions[0] + 1
        blocks = block_calls(calls[offset:], model_id, reason="batch_stopped_after_tool_rejection",
                             offset=offset)
        if not equal(tail[index + 1:], blocks):
            return None
    return {"reason": reason, "blocks": blocks,
            "skipped": {(event["model_call_id"], event["proposal_index"]) for event in blocks}}
