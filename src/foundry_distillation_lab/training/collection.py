"""Import actual exported traces, never synthesize successful cloud observations."""

from collections import defaultdict
import hashlib
from pathlib import Path

from ..io import canonical, read_json, read_jsonl, sha256, write_json, write_jsonl
from ..safety import nonnegative
from .execution import project_endpoint, target


def prepare_collection(input_path, config_path, output_dir):
    config = read_json(config_path)
    if set(config) != {"project_endpoint", "model", "max_output_tokens",
                       "conversation_seconds", "estimated_cost_per_model_request"}:
        raise ValueError("Collection config fields do not match the schema")
    config["project_endpoint"] = project_endpoint(config["project_endpoint"])
    target_id = target(config["project_endpoint"], config["model"])
    for key in ("max_output_tokens", "conversation_seconds"):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError("Collection token/time limits must be positive integers")
    nonnegative(config["estimated_cost_per_model_request"], "estimated_cost_per_model_request")
    if config["estimated_cost_per_model_request"] <= 0:
        raise ValueError("A conservative positive per-model-request reservation is required")
    inputs = []
    for row in read_jsonl(input_path):
        if set(row) != {"conversation_id", "category", "prompt"} or any(
                not isinstance(v, str) or not v.strip() for v in row.values()):
            raise ValueError("Prompt rows must contain conversation_id, category, prompt strings")
        inputs.append({**row, "input_sha256": hashlib.sha256(row["prompt"].encode()).hexdigest()})
    if not inputs or len({r["input_sha256"] for r in inputs}) != len(inputs):
        raise ValueError("Collection prompts must be nonempty and unique")
    if len({r["conversation_id"] for r in inputs}) != len(inputs):
        raise ValueError("Collection conversation IDs must be unique")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    plan = {"schema_version": 1, "kind": "collection", "target": target_id,
            "config": config, "inputs": inputs, "source_sha256": sha256(input_path)}
    write_json(output_dir / "collection-plan.json", plan)
    return plan


def _messages(items):
    messages = []
    for item in items:
        kind = item.get("type", "message")
        if kind == "reasoning":
            # Internal reasoning is not a training target or a business observation.
            continue
        if kind == "function_call":
            call = {"id": item["call_id"], "type": "function",
                    "function": {"name": item["name"], "arguments": item["arguments"]}}
            if messages and messages[-1]["role"] == "assistant":
                messages[-1].setdefault("tool_calls", []).append(call)
            else:
                messages.append({"role": "assistant", "tool_calls": [call]})
        elif kind == "function_call_output":
            output = item["output"]
            if not isinstance(output, str):
                raise ValueError("Only textual JSON tool results are supported")
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": output})
        elif kind == "message" and item.get("role") in {"system", "user", "assistant"}:
            content = item.get("content")
            if isinstance(content, list):
                if any(c.get("type") not in {"input_text", "output_text", "text"} for c in content):
                    raise ValueError("Only text trace content is supported")
                content = "".join(c["text"] for c in content)
            if not isinstance(content, str) or not content:
                raise ValueError("Missing trace message text")
            messages.append({"role": item["role"], "content": content})
        else:
            raise ValueError("Unsupported exported Responses item")
    return messages


def convert_capture(rows):
    grouped = defaultdict(list)
    for row in rows:
        if row.get("kind") != "foundry-responses-capture":
            raise ValueError("Expected foundry-responses-capture records")
        grouped[row["conversation_id"]].append(row)
    output, evidence = [], []
    for conversation_id, calls in grouped.items():
        calls.sort(key=lambda r: r["call_index"])
        if [r["call_index"] for r in calls] != list(range(1, len(calls) + 1)):
            raise ValueError("Missing or repeated model calls")
        last = calls[-1]
        request, response = last["request"], last["response"]
        if any(c["response"].get("status") != "completed" for c in calls):
            raise ValueError("Incomplete/failed model call; retain raw export for review")
        if request.get("previous_response_id"):
            raise ValueError("Server-side history export must be resolved before import")
        if not isinstance(request.get("input"), list):
            raise ValueError("Capture must include full local input history")
        messages = _messages(request["input"] + response["output"])
        instructions = request.get("instructions")
        if instructions:
            messages.insert(0, {"role": "system", "content": instructions})
        if not messages or messages[-1]["role"] != "assistant" or messages[-1].get("tool_calls"):
            raise ValueError("No terminal assistant answer captured")
        tools = []
        for tool in request.get("tools", []):
            if tool.get("type") != "function":
                raise ValueError("Only retail function tools are supported")
            tools.append({"type": "function", "function": {
                k: tool[k] for k in ("name", "description", "parameters")}})
        output.append({"conversation_id": conversation_id, "category": last["category"],
                       "source_kind": "hosted_response_capture_unreviewed",
                       "messages": messages, "tools": tools})
        evidence.append({"conversation_id": conversation_id,
                         "response_ids": [c["response"].get("id") for c in calls],
                         "known_usage": [c["response"].get("usage") for c in calls],
                         "actual_cost": None, "unknown_cost": True,
                         "model": request.get("model"), "sources": [c.get("source") for c in calls],
                         "model_calls": len(calls)})
    return output, evidence


def import_traces(input_path, output_dir, source_format):
    rows = read_jsonl(input_path)
    if not rows:
        raise ValueError("Trace export is empty")
    if source_format == "responses-capture":
        rows, evidence = convert_capture(rows)
    elif source_format == "conversation":
        evidence = []
        for row in rows:
            required = {"conversation_id", "category", "messages"}
            if not isinstance(row, dict) or not required <= set(row):
                raise ValueError("Conversation export requires conversation_id, category, messages")
            evidence.append({"conversation_id": row["conversation_id"],
                             "known_usage": row.get("usage"), "actual_cost": None,
                             "unknown_cost": True, "response_id": row.get("response_id"),
                             "model": row.get("model"), "source": row.get("source")})
        rows = [{**{k: r[k] for k in ("conversation_id", "category", "messages", "tools") if k in r},
                 "source_kind": r.get("source_kind", "exported_trace_unreviewed")} for r in rows]
    else:
        raise ValueError("Unknown trace export format")
    # Reuse the retail contract validator without importing any Azure SDK.
    from ..datasets.prepare import normalize
    normalized = [normalize(row)[0] for row in rows]
    if len({row["conversation_id"] for row in normalized}) != len(normalized):
        raise ValueError("Duplicate conversation IDs")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    write_jsonl(output_dir / "traces.jsonl", normalized)
    manifest = {"schema_version": 1, "source_format": source_format,
                "source_sha256": sha256(input_path), "trace_sha256": sha256(output_dir / "traces.jsonl"),
                "conversations": len(normalized), "observations": evidence,
                "source_kinds": sorted({row["source_kind"] for row in normalized}),
                "cloud_requests_made": 0, "quality_review": "required",
                "privacy_review": "required; regex checks are not sufficient"}
    write_json(output_dir / "collection-manifest.json", manifest)
    return manifest
