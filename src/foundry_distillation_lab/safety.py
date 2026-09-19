"""Local approval and durable reservation guards, not a cloud billing guarantee."""

from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path
import re

from .io import canonical, read_json, sha256, write_json


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Timestamp must be an ISO 8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed


def nonnegative(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return value


class Approval:
    def __init__(self, path, data):
        self.path = Path(path).resolve()
        self.data = data
        self.max_requests = data["max_requests"]
        self.max_cost = data["max_cost"]
        self._digest = sha256(self.path)

    @classmethod
    def load(cls, path, operation, input_path):
        data = read_json(path)
        if not isinstance(data, dict) or data.get("approved") is not True:
            raise ValueError("Explicit approval is required")
        if data.get("operation") != operation:
            raise ValueError("Approval operation does not match")
        if data.get("input_sha256") != sha256(input_path):
            raise ValueError("Approved input hash does not match")
        if not isinstance(data.get("target"), str) or not data["target"].strip():
            raise ValueError("Approval must identify a target")
        if type(data.get("max_requests")) is not int or data["max_requests"] < 1:
            raise ValueError("max_requests must be a positive integer")
        nonnegative(data.get("max_cost"), "max_cost")
        if not isinstance(data.get("currency"), str) or not re.fullmatch("[A-Z]{3}", data["currency"]):
            raise ValueError("Approval currency must be an ISO currency code")
        if timestamp(data.get("expires_at")) <= datetime.now(timezone.utc):
            raise ValueError("Approval has expired")
        return cls(path, data)

    def assert_target(self, target):
        if target != self.data["target"]:
            raise ValueError("Approved target does not match")

    def reserve(self, amount):
        nonnegative(amount, "reservation")
        if sha256(self.path) != self._digest:
            raise ValueError("Approval file changed")
        if timestamp(self.data["expires_at"]) <= datetime.now(timezone.utc):
            raise ValueError("Approval has expired")
        directory = self.path.parent / ".approval-state"
        directory.mkdir(parents=True, exist_ok=True)
        ledger = directory / (self._digest + ".json")
        lock = directory / (self._digest + ".lock")
        # An abandoned lock is deliberately not recovered automatically.
        with lock.open("x", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        try:
            state = read_json(ledger) if ledger.exists() else {"requests": 0, "reserved": 0}
            if type(state.get("requests")) is not int or state["requests"] < 0:
                raise ValueError("Invalid reservation ledger")
            nonnegative(state.get("reserved"), "ledger reservation")
            count, cost = state["requests"] + 1, state["reserved"] + amount
            if count > self.max_requests or cost > self.max_cost:
                raise ValueError("Approval request or cost limit would be exceeded")
            temporary = directory / (self._digest + ".pending")
            write_json(temporary, {"requests": count, "reserved": cost,
                                   "currency": self.data["currency"]})
            os.replace(temporary, ledger)
        finally:
            lock.unlink()


class Journal:
    def __init__(self, run_dir):
        self.directory = Path(run_dir) / "attempts"

    def _path(self, attempt_id, suffix):
        if not isinstance(attempt_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", attempt_id):
            raise ValueError("Invalid attempt ID")
        return self.directory / f"{attempt_id}.{suffix}.json"

    def start(self, attempt_id, payload):
        write_json(self._path(attempt_id, "started"), {
            "attempt_id": attempt_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "payload_sha256": hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest(),
            "status": "outcome_unknown_until_result",
        })

    def finish(self, attempt_id, status, result):
        if not self._path(attempt_id, "started").is_file():
            raise ValueError("Cannot finish an unstarted attempt")
        write_json(self._path(attempt_id, "result"), {
            "attempt_id": attempt_id, "status": status, "result": result,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })
