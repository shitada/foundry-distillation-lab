"""One attempt per request, with durable evidence before any transport is opened."""

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from ..io import canonical, read_json, sha256
from ..safety import Approval, Journal, nonnegative


def project_endpoint(value):
    if not isinstance(value, str):
        raise ValueError("An explicit Foundry project endpoint is required")
    parsed = urlsplit(value)
    parts = parsed.path.rstrip("/").split("/")
    if (parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith(".services.ai.azure.com")
            or parsed.username or parsed.password or parsed.port
            or parsed.query or parsed.fragment or len(parts) != 4
            or parts[1:3] != ["api", "projects"] or not parts[3]
            or any(c in parts[3] for c in "%\\ ")):
        raise ValueError("Expected https://<account>.services.ai.azure.com/api/projects/<project>")
    return value.rstrip("/")


def target(endpoint, model):
    if not isinstance(model, str) or not model.strip() or "|" in model:
        raise ValueError("An explicit model (including version when applicable) is required")
    return project_endpoint(endpoint) + "|" + model


def attempt_key(operation, payload):
    return operation + "-" + hashlib.sha256(canonical(payload).encode()).hexdigest()[:32]


def execute_once(*, execute, approval_path, operation, input_path, target_id,
                 run_dir, payload, estimated_cost, send, attempt_id=None):
    """`send` must lazily construct its client; never retry an uncertain operation."""
    if not execute:
        raise ValueError("Network access requires --execute")
    if approval_path is None:
        raise ValueError("Network access requires --approval")
    nonnegative(estimated_cost, "estimated_cost")
    approval = Approval.load(Path(approval_path), operation, Path(input_path))
    approval.assert_target(target_id)
    attempt_id = attempt_id or attempt_key(operation, payload)
    journal = Journal(Path(run_dir))
    # Existing starts prohibit resend, including transport failures and lost receipts.
    journal.start(attempt_id, {"operation": operation, "target": target_id,
                              "input_sha256": sha256(input_path), "request": payload})
    try:
        approval.reserve(estimated_cost)
    except Exception:
        journal.finish(attempt_id, "not_sent", {"reason": "reservation_rejected"})
        raise
    try:
        result = send()
    except BaseException as exc:
        journal.finish(attempt_id, "outcome_unknown", {
            "error_type": type(exc).__name__, "known_usage": None,
            "actual_cost": None, "unknown_cost": True,
        })
        raise
    journal.finish(attempt_id, "response_received", {
        "response": result, "known_usage": result.get("usage") or (
            {"trained_tokens": result["trained_tokens"]} if result.get("trained_tokens") is not None else None),
        "actual_cost": None, "unknown_cost": True,
    })
    return result


def checked_plan(path, kind):
    plan = read_json(path)
    if plan.get("kind") != kind or plan.get("schema_version") != 1:
        raise ValueError("Unexpected plan kind/version")
    return plan
