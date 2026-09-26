"""Durable write-ahead evidence and shared numeric validation."""

from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
import re

from .io import canonical, write_json


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
