"""Guarded one-shot azd invocation using the documented direct agent endpoint."""

import hashlib
import os
from pathlib import Path
import re
import subprocess

from ..io import parse_json, read_json, sha256, write_json
from .execution import checked_plan, execute_once, target


def validate(plan):
    if (plan.get("schema_version") != 1 or plan.get("kind") != "hosted-invocation"
            or set(plan) != {"schema_version", "kind", "collection_plan_sha256", "model_target",
                             "project_endpoint", "model", "agent_endpoint", "agent_version",
                             "target", "input", "timeout_seconds"}):
        raise ValueError("Invalid hosted invocation plan")
    if plan["model_target"] != target(plan["project_endpoint"], plan["model"]):
        raise ValueError("Model target mismatch")
    endpoint = plan["agent_endpoint"]
    prefix = plan["project_endpoint"].rstrip("/") + "/agents/"
    if (not isinstance(endpoint, str) or not endpoint.startswith(prefix)
            or not re.fullmatch(r"[A-Za-z0-9_-]+/versions/[1-9][0-9]*", endpoint[len(prefix):])
            or endpoint.rsplit("/", 1)[1] != plan["agent_version"]
            or plan["target"] != endpoint):
        raise ValueError("Use the exact versioned agent endpoint from the configured project")
    if type(plan["timeout_seconds"]) is not int or not 1 <= plan["timeout_seconds"] <= 3600:
        raise ValueError("timeout_seconds must be 1..3600")
    item = plan["input"]
    if (set(item) != {"conversation_id", "category", "prompt", "input_sha256"}
            or not all(isinstance(value, str) and value for value in item.values())
            or hashlib.sha256(item["prompt"].encode()).hexdigest() != item["input_sha256"]):
        raise ValueError("Invocation prompt hash mismatch")
    return plan


def prepare(collection_plan_path, config_path, output_dir):
    collection = checked_plan(collection_plan_path, "collection")
    config = read_json(config_path)
    if set(config) != {"conversation_id", "agent_endpoint", "agent_version",
                       "timeout_seconds"}:
        raise ValueError("Invalid invocation configuration")
    matches = [item for item in collection["inputs"]
               if item["conversation_id"] == config["conversation_id"]]
    if len(matches) != 1:
        raise ValueError("Select exactly one collection input")
    plan = validate({"schema_version": 1, "kind": "hosted-invocation",
        "collection_plan_sha256": sha256(collection_plan_path), "model_target": collection["target"],
        "project_endpoint": collection["config"]["project_endpoint"],
        "model": collection["config"]["model"], "agent_endpoint": config["agent_endpoint"],
        "agent_version": config["agent_version"], "target": config["agent_endpoint"],
        "input": matches[0], "timeout_seconds": config["timeout_seconds"]})
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    write_json(output_dir / "invocation-plan.json", plan)
    return plan


def command(plan):
    validate(plan)
    return ["azd", "ai", "agent", "invoke", plan["input"]["prompt"],
            "--agent-endpoint", plan["agent_endpoint"], "--version", plan["agent_version"],
            "--protocol", "responses", "--new-session", "--output", "raw",
            "--timeout", str(plan["timeout_seconds"]), "--no-prompt"]


def parse_response(raw):
    normalized = raw.replace("\r\n", "\n")
    match = re.match(r"HTTP/\S+\s+([0-9]{3})[^\n]*\n", normalized)
    if not match or "\n\n" not in normalized:
        raise ValueError("Unrecognized azd raw HTTP output; outcome unknown, do not resend")
    status = int(match.group(1))
    body = normalized.split("\n\n", 1)[1].strip()
    if body.startswith("{"):
        response = parse_json(body)
    else:
        response = None
        for line in body.splitlines():
            if line.startswith("data: ") and line[6:] != "[DONE]":
                event = parse_json(line[6:])
                if event.get("type") in {"response.completed", "response.failed", "response.incomplete"}:
                    response = event["response"]
        if response is None:
            raise ValueError("Terminal response absent; outcome unknown, do not resend")
    return {"http_status": status, "response": response, "usage": response.get("usage"),
            "capture_retrieval": "not_verified"}


class AzdTransport:
    def invoke(self, plan):
        env = os.environ.copy()
        env["AZURE_DEV_USER_AGENT"] = "microsoft_foundry_skill"
        result = subprocess.run(command(plan), shell=False, capture_output=True,
            encoding="utf-8", errors="strict", check=False, env=env,
            timeout=plan["timeout_seconds"])
        if result.returncode != 0:
            raise RuntimeError("azd invocation failed; outcome unknown; stderr not persisted")
        return parse_response(result.stdout)


def invoke(plan_path, *, run_dir, transport=None):
    plan = validate(checked_plan(plan_path, "hosted-invocation"))
    transport = transport or AzdTransport()
    result = execute_once(operation="collect",
        input_path=plan_path, target_id=plan["target"], run_dir=run_dir, payload=plan,
        send=lambda: transport.invoke(plan))
    receipt = {"schema_version": 1, "kind": "hosted-invocation-receipt",
               "plan_sha256": sha256(plan_path), "target": plan["target"],
               "input_sha256": plan["input"]["input_sha256"],
               "conversation_id": plan["input"]["conversation_id"],
               "response": result, "trace_capture_verified": False}
    write_json(Path(run_dir) / "invocation-receipt.json", receipt)
    return receipt
