"""SFT request preparation and lazy, no-retry Foundry SDK transport."""

from contextlib import contextmanager
import codecs
import hashlib
import io
from pathlib import Path
import re
import time
import uuid

from ..io import canonical, parse_json, read_json, sha256, write_json
from ..safety import nonnegative
from .execution import attempt_key, checked_plan, execute_once, project_endpoint, target


MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def validate_rows(raw):
    rows = []
    for line in raw.decode("utf-8-sig").splitlines():
        if not line.strip():
            raise ValueError("Blank JSONL records are not accepted")
        row = parse_json(line)
        if not isinstance(row, dict) or set(row) - {"messages", "tools", "parallel_tool_calls"}:
            raise ValueError("Training rows may contain messages, tools, parallel_tool_calls only")
        messages = row.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("Training messages are required")
        if any(not isinstance(m, dict) or m.get("role") not in
               {"system", "user", "assistant", "tool"} for m in messages):
            raise ValueError("Invalid training message role")
        for message in messages:
            if set(message) - {"role", "content", "tool_calls", "tool_call_id", "weight"}:
                raise ValueError("Unsupported training message field")
            for call in message.get("tool_calls", []):
                if (not isinstance(call, dict) or set(call) != {"id", "type", "function"}
                        or call["type"] != "function"
                        or set(call["function"]) != {"name", "arguments"}):
                    raise ValueError("Unsupported training tool call")
        if not any(m["role"] == "user" for m in messages) or messages[-1]["role"] != "assistant":
            raise ValueError("Training rows require a user input and final assistant target")
        if "tools" in row and not isinstance(row["tools"], list):
            raise ValueError("tools must be a list")
        if "tools" in row:
            from ..retail import RetailSession
            if row["tools"] != RetailSession().tools:
                raise ValueError("Only the frozen retail tool contract may be uploaded")
        rows.append(row)
    if not rows:
        raise ValueError("Empty training data")
    return rows


def training_config(config):
    allowed = {"project_endpoint", "model", "training_type", "hyperparameters", "seed",
               "suffix"}
    if not isinstance(config, dict) or set(config) - allowed:
        raise ValueError("Unknown training config field")
    if not {"project_endpoint", "model", "training_type"} <= set(config):
        raise ValueError("Training config requires project_endpoint, model, and training_type")
    result = dict(config)
    result["project_endpoint"] = project_endpoint(config["project_endpoint"])
    target(result["project_endpoint"], config["model"])
    if config.get("training_type") not in {"Standard", "GlobalStandard", "Developer"}:
        raise ValueError("Specify Standard, GlobalStandard, or Developer training_type")
    hp = config.get("hyperparameters", {})
    if not isinstance(hp, dict) or set(hp) - {"n_epochs", "batch_size", "learning_rate_multiplier"}:
        raise ValueError("Unsupported supervised hyperparameter")
    for name, value in hp.items():
        if name in {"n_epochs", "batch_size"}:
            if type(value) is not int or value <= 0:
                raise ValueError("Integer hyperparameters must be positive; omit for automatic defaults")
        elif type(value) not in (int, float) or value <= 0:
            raise ValueError("learning_rate_multiplier must be positive")
        nonnegative(value, name)
    result["hyperparameters"] = hp
    if "seed" in config and type(config["seed"]) is not int:
        raise ValueError("seed must be an integer")
    if "suffix" in config and (not isinstance(config["suffix"], str)
                              or not re.fullmatch(r"[A-Za-z0-9_-]{1,18}", config["suffix"])):
        raise ValueError("suffix must have 1-18 letters, digits, underscores or hyphens")
    return result


def prepare(train_path, validation_path, config_path, output_dir):
    config = training_config(read_json(config_path))
    sources = {}
    for name, path in (("train", train_path), ("validation", validation_path)):
        raw = Path(path).read_bytes()
        rows = validate_rows(raw)
        normalized = codecs.BOM_UTF8 + "".join(canonical(r) + "\n" for r in rows).encode("utf-8")
        if len(normalized) >= MAX_UPLOAD_BYTES:
            raise ValueError("Each upload must be smaller than 512 MB")
        sources[name] = (raw, rows, normalized)
    if len(sources["train"][1]) < 10:
        raise ValueError("SFT requires at least 10 training examples")
    train_hashes = {canonical(row) for row in sources["train"][1]}
    if any(canonical(row) in train_hashes for row in sources["validation"][1]):
        raise ValueError("Exact training/validation overlap")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    write_json(output_dir / "config.json", config)
    files = {}
    for name, (raw, rows, normalized) in sources.items():
        path = output_dir / f"{name}.jsonl"
        with path.open("xb") as stream:
            stream.write(normalized)
        files[name] = {"path": path.name, "sha256": sha256(path), "bytes": len(normalized),
                       "rows": len(rows), "source_sha256": hashlib.sha256(raw).hexdigest()}
    plan = {"schema_version": 1, "kind": "training-upload", "config": config, "files": files,
            "config_sha256": sha256(config_path),
            "snapshot_config_sha256": sha256(output_dir / "config.json"),
            "target": target(config["project_endpoint"], config["model"]),
            "validation_scope": "structural_only; run dataset contract/leakage validation separately"}
    write_json(output_dir / "upload-plan.json", plan)
    return plan


@contextmanager
def sdk_client(endpoint):
    # Optional root cloud dependencies, never imported by offline commands.
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    with DefaultAzureCredential() as credential:
        with AIProjectClient(endpoint=endpoint, credential=credential) as project:
            with project.get_openai_client() as client:
                client.max_retries = 0
                client.timeout = 60.0
                yield client


class TrainingTransport:
    def __init__(self, endpoint):
        self.endpoint = project_endpoint(endpoint)

    def upload(self, name, raw):
        with sdk_client(self.endpoint) as client:
            return client.files.create(file=(name, io.BytesIO(raw)),
                                       purpose="fine-tune").model_dump(mode="json")

    def submit(self, payload):
        with sdk_client(self.endpoint) as client:
            return client.fine_tuning.jobs.create(**payload).model_dump(mode="json")

    def status(self, kind, identifier):
        with sdk_client(self.endpoint) as client:
            if kind == "file":
                return client.files.retrieve(identifier).model_dump(mode="json")
            return client.fine_tuning.jobs.retrieve(identifier).model_dump(mode="json")


def _upload_plan(plan_path):
    plan = checked_plan(plan_path, "training-upload")
    config = training_config(plan["config"])
    if plan["target"] != target(config["project_endpoint"], config["model"]):
        raise ValueError("Invalid upload target")
    snapshot = Path(plan_path).parent / "config.json"
    if (sha256(snapshot) != plan["snapshot_config_sha256"]
            or read_json(snapshot) != config):
        raise ValueError("Prepared configuration changed")
    for role in ("train", "validation"):
        _upload_data(plan_path, plan, role)
    return plan


def _upload_data(plan_path, plan, role):
    item = plan["files"][role]
    if item["path"] != f"{role}.jsonl":
        raise ValueError("Unexpected upload filename")
    raw = (Path(plan_path).parent / item["path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != item["sha256"]:
        raise ValueError("Prepared file hash changed")
    if not raw.startswith(codecs.BOM_UTF8) or len(raw) >= MAX_UPLOAD_BYTES:
        raise ValueError("Prepared files require UTF-8 BOM and must be smaller than 512 MB")
    validate_rows(raw)
    return raw


def _upload_payload(plan, role):
    return {"role": role, "file": plan["files"][role], "purpose": "fine-tune"}


def upload(plan_path, role, *, run_dir, transport=None):
    if role not in {"train", "validation"}:
        raise ValueError("Invalid upload role")
    plan = _upload_plan(plan_path)
    config = plan["config"]
    expected_target = plan["target"]
    item = plan["files"][role]
    raw = _upload_data(plan_path, plan, role)
    payload = _upload_payload(plan, role)
    transport = transport or TrainingTransport(config["project_endpoint"])
    result = execute_once(
        operation="training-upload", input_path=plan_path, target_id=expected_target,
        run_dir=run_dir, payload=payload,
        send=lambda: transport.upload(item["path"], raw))
    _identifier(result.get("id"))
    receipt = {"schema_version": 1, "kind": "training-file-receipt", "role": role,
               "target": expected_target, "project_endpoint": config["project_endpoint"],
               "model": config["model"], "plan_sha256": sha256(plan_path),
               "file_sha256": item["sha256"], "response": result}
    write_json(Path(run_dir) / f"{role}-upload-receipt.json", receipt)
    return receipt


def prepare_submission(upload_plan_path, train_receipt, validation_receipt, output):
    plan = _upload_plan(upload_plan_path)
    config = training_config(plan["config"])
    payload = {"model": config["model"], "method": {"type": "supervised",
               "supervised": {"hyperparameters": config["hyperparameters"]}},
               "extra_body": {"trainingType": config["training_type"]}}
    receipts = {}
    for role, path, key in (("train", train_receipt, "training_file"),
                             ("validation", validation_receipt, "validation_file")):
        receipt = checked_plan(path, "training-file-receipt")
        if (receipt["role"] != role or receipt["target"] != plan["target"]
                or receipt["plan_sha256"] != sha256(upload_plan_path)
                or receipt["file_sha256"] != plan["files"][role]["sha256"]):
            raise ValueError("Upload receipt does not match prepared dataset")
        payload[key] = receipt["response"]["id"]
        receipts[role] = {"sha256": sha256(path), "file_sha256": receipt["file_sha256"]}
    for key in ("suffix", "seed"):
        if key in config:
            payload[key] = config[key]
    result = {"schema_version": 1, "kind": "training-submit", "target": plan["target"],
              "project_endpoint": config["project_endpoint"], "payload": payload,
              "receipts": receipts,
              "upload_plan_sha256": sha256(upload_plan_path)}
    write_json(output, result)
    return result


def submit(plan_path, *, run_dir, transport=None, timeout_seconds=3600, poll_seconds=10,
           sleep=time.sleep, clock=time.monotonic):
    _poll_options(timeout_seconds, poll_seconds)
    plan = checked_plan(plan_path, "training-submit")
    payload = plan["payload"]
    expected_target = target(plan["project_endpoint"], payload["model"])
    if expected_target != plan["target"]:
        raise ValueError("Submission target mismatch")
    allowed = {"model", "method", "training_file", "validation_file", "seed", "suffix", "extra_body"}
    if set(payload) - allowed or payload.get("method", {}).get("type") != "supervised":
        raise ValueError("Only explicit supervised training requests are supported")
    if set(payload.get("extra_body", {})) != {"trainingType"}:
        raise ValueError("Automatic deployment or extra operations are not permitted")
    method = payload["method"]
    if set(method) != {"type", "supervised"} or set(method["supervised"]) != {"hyperparameters"}:
        raise ValueError("Unsupported training method fields")
    training_config({"project_endpoint": plan["project_endpoint"], "model": payload["model"],
        "training_type": payload["extra_body"]["trainingType"],
        "hyperparameters": method["supervised"]["hyperparameters"],
        **{key: payload[key] for key in ("seed", "suffix") if key in payload}})
    for key in ("training_file", "validation_file"):
        if not isinstance(payload.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", payload[key]):
            raise ValueError("Explicit uploaded file IDs are required")
    transport = transport or TrainingTransport(plan["project_endpoint"])
    attempt_id = attempt_key("training-submit", payload)
    if (Path(run_dir) / "attempts" / f"{attempt_id}.started.json").exists():
        raise FileExistsError("Submission already attempted; use status for GET-only reconciliation")
    wait_for_files(plan_path, plan, run_dir=run_dir, transport=transport,
                   timeout_seconds=timeout_seconds, poll_seconds=poll_seconds,
                   sleep=sleep, clock=clock)
    result = execute_once(
        operation="training-submit", input_path=plan_path, target_id=expected_target,
        run_dir=run_dir, payload=payload,
        send=lambda: transport.submit(payload))
    _identifier(result.get("id"))
    receipt = {"schema_version": 1, "kind": "training-job-receipt",
               "target": expected_target, "project_endpoint": plan["project_endpoint"],
               "model": payload["model"], "response": result, "plan_sha256": sha256(plan_path)}
    write_json(Path(run_dir) / "job-receipt.json", receipt)
    return receipt


def _identifier(identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        raise ValueError("Missing/invalid remote ID; reconcile with the service, never blindly resend")
    return identifier


def _poll_options(timeout_seconds, poll_seconds):
    for name, value in (("timeout_seconds", timeout_seconds), ("poll_seconds", poll_seconds)):
        nonnegative(value, name)
        if value == 0:
            raise ValueError(f"{name} must be positive")


def _observe(input_path, plan, kind, identifier, run_dir, transport):
    _identifier(identifier)
    observation_id = uuid.uuid4().hex
    response = execute_once(
        operation="training-status", input_path=input_path, target_id=plan["target"], run_dir=run_dir,
        payload={"method": "GET", "kind": kind, "id": identifier},
        send=lambda: transport.status(kind, identifier), attempt_id="status-" + observation_id)
    write_json(Path(run_dir) / "observations" / f"{observation_id}.json",
               {"kind": kind, "id": identifier, "response": response})
    if response.get("id") != identifier:
        raise ValueError("Status response ID does not match the requested resource")
    states = {
        "job": {"pending", "validating_files", "queued", "running", "cancelling",
                "succeeded", "failed", "cancelled"},
        "file": {"uploaded", "pending", "processing", "processed",
                 "error", "failed", "cancelled", "deleted"},
    }
    state = response.get("status")
    if not isinstance(state, str) or state not in states[kind]:
        raise ValueError(f"Unrecognized {kind} status for {identifier}: {state!r}; "
                         "observation saved, but no running/completed state is confirmed")
    return response


def _pause(deadline, poll_seconds, sleep, clock):
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError("Polling deadline exceeded; no success is implied. Use train.py status.")
    sleep(min(poll_seconds, remaining))


def wait_for_files(plan_path, plan, *, run_dir, transport, timeout_seconds=3600,
                   poll_seconds=10, sleep=time.sleep, clock=time.monotonic):
    _poll_options(timeout_seconds, poll_seconds)
    deadline = clock() + timeout_seconds
    pending = {plan["payload"][key] for key in ("training_file", "validation_file")}
    while pending:
        for identifier in sorted(pending):
            if clock() >= deadline:
                raise TimeoutError("File processing deadline exceeded; training was not submitted")
            response = _observe(plan_path, plan, "file", identifier, run_dir, transport)
            state = response.get("status")
            if state in {"error", "failed", "cancelled", "deleted"}:
                raise ValueError(f"File {identifier} processing failed: {state}; training was not submitted")
            if clock() >= deadline:
                raise TimeoutError("File processing deadline exceeded; training was not submitted")
            if state == "processed":
                pending.remove(identifier)
        if pending:
            _pause(deadline, poll_seconds, sleep, clock)


def start(train_path, validation_path, config_path, run_dir, *, transport=None,
          timeout_seconds=3600, poll_seconds=10, sleep=time.sleep, clock=time.monotonic):
    """Validate and snapshot locally, upload twice, wait for processing, then submit once."""
    _poll_options(timeout_seconds, poll_seconds)
    run_dir = Path(run_dir)
    # The create-only directory prevents a restarted command from replaying any mutation.
    prepare(train_path, validation_path, config_path, run_dir)
    plan_path = run_dir / "upload-plan.json"
    for role in ("train", "validation"):
        upload(plan_path, role, run_dir=run_dir, transport=transport)
    submit_path = run_dir / "submit-plan.json"
    prepare_submission(plan_path, run_dir / "train-upload-receipt.json",
                       run_dir / "validation-upload-receipt.json", submit_path)
    return submit(submit_path, run_dir=run_dir, transport=transport,
                  timeout_seconds=timeout_seconds, poll_seconds=poll_seconds,
                  sleep=sleep, clock=clock)


def _known_response(run_dir, operation, payload, receipt_name, diagnostics):
    result_path = Path(run_dir) / "attempts" / f"{attempt_key(operation, payload)}.result.json"
    receipt_path = Path(run_dir) / receipt_name
    # Prefer the write-ahead response: a later receipt save may have been interrupted.
    known = None
    for path in (result_path, receipt_path):
        try:
            evidence = read_json(path)
            if not isinstance(evidence, dict):
                raise ValueError("Evidence must be a JSON object")
            if path == result_path:
                state = evidence.get("status")
                if not isinstance(evidence.get("result"), dict):
                    raise ValueError("Journal result must be a JSON object")
                if isinstance(state, str) and state in {"outcome_unknown", "not_sent"}:
                    continue
                if state != "response_received":
                    raise ValueError(f"Unrecognized journal outcome: {state!r}")
                evidence = evidence["result"]
            else:
                kind = "training-job-receipt" if operation == "training-submit" else "training-file-receipt"
                if evidence.get("kind") != kind or evidence.get("schema_version") != 1:
                    raise ValueError("Unexpected receipt kind/version")
            response = evidence.get("response")
            if not isinstance(response, dict):
                raise ValueError("Evidence response must be a JSON object")
            _identifier(response.get("id"))
            if known is not None and response["id"] != known["id"]:
                raise ValueError("Receipt ID conflicts with durable journal response; using journal ID")
            if known is None:
                known = response
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            diagnostics.append({"path": str(path), "reason": f"{type(exc).__name__}: {exc}"})
    return known


def status(run_dir, *, wait=False, timeout_seconds=3600, poll_seconds=10,
           transport=None, sleep=time.sleep, clock=time.monotonic):
    """Repeatable read-only reconciliation, including a missing local submission receipt."""
    _poll_options(timeout_seconds, poll_seconds)
    run_dir = Path(run_dir)
    plan_path = run_dir / "upload-plan.json"
    plan = checked_plan(plan_path, "training-upload")
    config = training_config(plan["config"])
    if plan["target"] != target(config["project_endpoint"], config["model"]):
        raise ValueError("Invalid run target")
    resources = {}
    diagnostics = []
    submit_path = run_dir / "submit-plan.json"
    if submit_path.exists():
        submission = checked_plan(submit_path, "training-submit")
        if submission["target"] != plan["target"]:
            raise ValueError("Submission target mismatch")
        job = _known_response(run_dir, "training-submit", submission["payload"],
                              "job-receipt.json", diagnostics)
        if job and job.get("id"):
            resources["job"] = ("job", _identifier(job["id"]))
    if not resources:
        for role in ("train", "validation"):
            response = _known_response(run_dir, "training-upload", _upload_payload(plan, role),
                                       f"{role}-upload-receipt.json", diagnostics)
            if response and response.get("id"):
                resources[role] = ("file", _identifier(response["id"]))
    transport = transport or TrainingTransport(config["project_endpoint"])
    deadline = clock() + timeout_seconds
    latest = {}

    def result(outcome, message=None):
        value = {"outcome": outcome, "states": latest, "diagnostics": diagnostics}
        if message is not None:
            value["message"] = message
        return value

    while resources:
        for name, (kind, identifier) in resources.items():
            if clock() >= deadline:
                return result("timeout", "Status polling timed out; completion is not confirmed.")
            latest[name] = _observe(plan_path, plan, kind, identifier, run_dir, transport)
        if clock() >= deadline:
            return result("timeout", "Status polling timed out; completion is not confirmed.")
        if "job" in latest:
            state = latest["job"].get("status")
            if state in {"succeeded", "failed", "cancelled"}:
                return result(state)
            if not wait:
                return result("pending")
        else:
            if any(value.get("status") in {"error", "failed", "cancelled", "deleted"}
                   for value in latest.values()):
                return result("failed", "File processing failed; no training job is known.")
            if not wait or all(value.get("status") == "processed" for value in latest.values()):
                break
        try:
            _pause(deadline, poll_seconds, sleep, clock)
        except TimeoutError:
            return result("timeout", "Status polling timed out; completion is not confirmed.")
    return result("no_job",
                  "No known job ID. Inspect attempts and service files/jobs to reconcile this run. "
                  "Status never uploads or submits; do not blindly rerun an uncertain mutation.")
