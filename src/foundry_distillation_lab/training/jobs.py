"""SFT request preparation and lazy, no-retry Foundry SDK transport."""

from contextlib import contextmanager
import codecs
import hashlib
import io
from pathlib import Path
import re

from ..io import canonical, parse_json, read_json, sha256, write_json
from ..safety import nonnegative
from .execution import checked_plan, execute_once, project_endpoint, target


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
               "suffix", "upload_estimated_cost", "submit_estimated_cost"}
    if set(config) - allowed:
        raise ValueError("Unknown training config field")
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
    if "suffix" in config and not re.fullmatch(r"[A-Za-z0-9_-]{1,18}", config["suffix"]):
        raise ValueError("suffix must have 1-18 letters, digits, underscores or hyphens")
    for key in ("upload_estimated_cost", "submit_estimated_cost"):
        nonnegative(config.get(key), key)
    if config["submit_estimated_cost"] <= 0:
        raise ValueError("Training requires a positive conservative cost reservation")
    return result


def prepare(train_path, validation_path, config_path, output_dir):
    config = training_config(read_json(config_path))
    sources = {}
    for name, path in (("train", train_path), ("validation", validation_path)):
        raw = Path(path).read_bytes()
        rows = validate_rows(raw)
        normalized = codecs.BOM_UTF8 + "".join(canonical(r) + "\n" for r in rows).encode("utf-8")
        if len(normalized) >= 512 * 1024 * 1024:
            raise ValueError("Each upload must be smaller than 512 MB")
        sources[name] = (raw, rows, normalized)
    if len(sources["train"][1]) < 10:
        raise ValueError("SFT requires at least 10 training examples")
    train_hashes = {canonical(row) for row in sources["train"][1]}
    if any(canonical(row) in train_hashes for row in sources["validation"][1]):
        raise ValueError("Exact training/validation overlap")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    files = {}
    for name, (raw, rows, normalized) in sources.items():
        path = output_dir / f"{name}.jsonl"
        with path.open("xb") as stream:
            stream.write(normalized)
        files[name] = {"path": path.name, "sha256": sha256(path), "bytes": len(normalized),
                       "rows": len(rows), "source_sha256": hashlib.sha256(raw).hexdigest()}
    plan = {"schema_version": 1, "kind": "training-upload", "config": config, "files": files,
            "config_sha256": sha256(config_path),
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


def upload(plan_path, role, *, execute=False, approval_path=None, run_dir, transport=None):
    plan = checked_plan(plan_path, "training-upload")
    config = training_config(plan["config"])
    expected_target = target(config["project_endpoint"], config["model"])
    if plan["target"] != expected_target or role not in {"train", "validation"}:
        raise ValueError("Invalid upload target or role")
    item = plan["files"][role]
    if item["path"] != f"{role}.jsonl":
        raise ValueError("Unexpected upload filename")
    raw = (Path(plan_path).parent / item["path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != item["sha256"]:
        raise ValueError("Prepared file hash changed")
    validate_rows(raw)
    payload = {"role": role, "file": item, "purpose": "fine-tune"}
    transport = transport or TrainingTransport(config["project_endpoint"])
    result = execute_once(execute=execute, approval_path=approval_path,
        operation="training-upload", input_path=plan_path, target_id=expected_target,
        run_dir=run_dir, payload=payload, estimated_cost=config["upload_estimated_cost"],
        send=lambda: transport.upload(item["path"], raw))
    if not isinstance(result.get("id"), str) or not result["id"]:
        raise ValueError("Upload response has no file ID; use GET-only reconciliation")
    receipt = {"schema_version": 1, "kind": "training-file-receipt", "role": role,
               "target": expected_target, "project_endpoint": config["project_endpoint"],
               "model": config["model"], "plan_sha256": sha256(plan_path),
               "file_sha256": item["sha256"], "response": result}
    write_json(Path(run_dir) / f"{role}-upload-receipt.json", receipt)
    return receipt


def prepare_submission(upload_plan_path, train_receipt, validation_receipt, output):
    plan = checked_plan(upload_plan_path, "training-upload")
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
            raise ValueError("Upload receipt does not match approved dataset")
        payload[key] = receipt["response"]["id"]
        receipts[role] = {"sha256": sha256(path), "file_sha256": receipt["file_sha256"]}
    for key in ("suffix", "seed"):
        if key in config:
            payload[key] = config[key]
    result = {"schema_version": 1, "kind": "training-submit", "target": plan["target"],
              "project_endpoint": config["project_endpoint"], "payload": payload,
              "estimated_cost": config["submit_estimated_cost"], "receipts": receipts,
              "upload_plan_sha256": sha256(upload_plan_path)}
    write_json(output, result)
    return result


def submit(plan_path, *, execute=False, approval_path=None, run_dir, transport=None):
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
        "upload_estimated_cost": 0, "submit_estimated_cost": plan["estimated_cost"],
        **{key: payload[key] for key in ("seed", "suffix") if key in payload}})
    for key in ("training_file", "validation_file"):
        if not isinstance(payload.get(key), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", payload[key]):
            raise ValueError("Explicit uploaded file IDs are required")
    transport = transport or TrainingTransport(plan["project_endpoint"])
    result = execute_once(execute=execute, approval_path=approval_path,
        operation="training-submit", input_path=plan_path, target_id=expected_target,
        run_dir=run_dir, payload=payload, estimated_cost=plan["estimated_cost"],
        send=lambda: transport.submit(payload))
    receipt = {"schema_version": 1, "kind": "training-job-receipt",
               "target": expected_target, "project_endpoint": plan["project_endpoint"],
               "model": payload["model"], "response": result, "plan_sha256": sha256(plan_path)}
    write_json(Path(run_dir) / "job-receipt.json", receipt)
    return receipt


def status(receipt_path, *, execute=False, approval_path=None, run_dir,
           observation_id, transport=None):
    receipt = read_json(receipt_path)
    kinds = {"training-job-receipt": "job", "training-file-receipt": "file"}
    if receipt.get("kind") not in kinds:
        raise ValueError("Status requires a known file/job receipt")
    identifier = receipt["response"]["id"]
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        raise ValueError("Invalid remote identifier")
    kind = kinds[receipt["kind"]]
    transport = transport or TrainingTransport(receipt["project_endpoint"])
    expected_target = target(receipt["project_endpoint"], receipt["model"])
    if expected_target != receipt["target"]:
        raise ValueError("Receipt target mismatch")
    return execute_once(execute=execute, approval_path=approval_path,
        operation="training-submit" if kind == "job" else "training-upload",
        input_path=receipt_path, target_id=expected_target, run_dir=run_dir,
        payload={"method": "GET", "kind": kind, "id": identifier},
        estimated_cost=0, send=lambda: transport.status(kind, identifier),
        attempt_id="status-" + observation_id)
