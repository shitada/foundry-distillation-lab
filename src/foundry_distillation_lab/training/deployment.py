"""Account deployment payloads; create-only intent and manifest-bound cleanup."""

import json
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from ..io import read_json, write_json
from .execution import attempt_key, checked_plan, execute_once

API_VERSION = "2024-10-01"
RESOURCE = re.compile(
    r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[A-Za-z0-9_.()-]+"
    r"/providers/Microsoft\.CognitiveServices/accounts/[A-Za-z0-9_.-]+"
    r"/deployments/[A-Za-z0-9_-]+")


def build_plan(config):
    required = {"subscription_id", "resource_group", "account", "deployment_prefix",
                "model_name", "model_version", "sku", "capacity"}
    if set(config) != required:
        raise ValueError("Deployment config fields must exactly match the documented schema")
    if str(uuid.UUID(config["subscription_id"])) != config["subscription_id"].lower():
        raise ValueError("Invalid subscription UUID")
    for key in ("resource_group", "account", "deployment_prefix"):
        if not isinstance(config[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", config[key]):
            raise ValueError(f"Invalid {key}")
    for key in ("model_name", "model_version", "sku"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"Explicit {key} required")
    if type(config["capacity"]) is not int or config["capacity"] < 1:
        raise ValueError("capacity must be a positive integer")
    owner = uuid.uuid4().hex
    name = config["deployment_prefix"] + "-" + owner[:12]
    resource_id = (f"/subscriptions/{config['subscription_id']}/resourceGroups/"
                   f"{config['resource_group']}/providers/Microsoft.CognitiveServices/"
                   f"accounts/{config['account']}/deployments/{name}")
    return {"schema_version": 1, "kind": "deployment", "api_version": API_VERSION,
            "resource_id": resource_id, "owner": owner,
            "body": {"tags": {"distillation-lab-owner": owner},
                     "sku": {"name": config["sku"], "capacity": config["capacity"]},
                     "properties": {"model": {"format": "OpenAI", "name": config["model_name"],
                                               "version": config["model_version"]},
                                    "versionUpgradeOption": "NoAutoUpgrade"}}}


def validate_plan(plan):
    if (set(plan) != {"schema_version", "kind", "api_version", "resource_id", "owner", "body"}
            or plan.get("schema_version") != 1 or plan.get("kind") != "deployment"
            or plan.get("api_version") != API_VERSION
            or not RESOURCE.fullmatch(plan.get("resource_id", ""))
            or not re.fullmatch("[0-9a-f]{32}", plan.get("owner", ""))
            or not plan["resource_id"].endswith("-" + plan["owner"][:12])):
        raise ValueError("Invalid deployment plan")
    body = plan["body"]
    if (set(body) != {"sku", "properties", "tags"}
            or body["tags"] != {"distillation-lab-owner": plan["owner"]}
            or set(body["properties"]) != {"model", "versionUpgradeOption"}
            or body["properties"]["versionUpgradeOption"] != "NoAutoUpgrade"
            or body["properties"]["model"].get("format") != "OpenAI"):
        raise ValueError("Deployment plan contains unsupported properties")
    model = body["properties"]["model"]
    if (set(model) != {"format", "name", "version"}
            or any(not isinstance(model[k], str) or not model[k].strip() for k in model)
            or set(body["sku"]) != {"name", "capacity"}
            or not isinstance(body["sku"]["name"], str) or not body["sku"]["name"]
            or type(body["sku"]["capacity"]) is not int or body["sku"]["capacity"] < 1):
        raise ValueError("Explicit model/version and positive SKU capacity are required")
    return plan


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("ARM redirects are not followed")


class ArmTransport:
    def request(self, method, resource_id, body=None, headers=None):
        if method not in {"GET", "PUT", "DELETE"} or not RESOURCE.fullmatch(resource_id):
            raise ValueError("Unsupported ARM operation or resource")
        from azure.identity import DefaultAzureCredential

        with DefaultAzureCredential() as credential:
            token = credential.get_token("https://management.azure.com/.default").token
            request_headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
            request_headers.update(headers or {})
            url = "https://management.azure.com" + resource_id + "?api-version=" + API_VERSION
            request = Request(url, method=method, headers=request_headers,
                              data=None if body is None else json.dumps(body).encode())
            try:
                response = build_opener(_NoRedirect()).open(request, timeout=60)
            except HTTPError as exc:
                response = exc
            with response:
                raw = response.read()
                parsed = json.loads(raw) if raw else {}
                return {"http_status": response.status, "body": parsed,
                        "etag": response.headers.get("ETag") or parsed.get("etag")}


def _request(plan, method, *, operation, input_path, run_dir,
             transport, body=None, headers=None, attempt_id=None):
    payload = {"method": method, "resource_id": plan["resource_id"],
               "body": body, "headers": headers or {}}
    return execute_once(operation=operation, input_path=input_path, target_id=plan["resource_id"],
        run_dir=run_dir, payload=payload,
        send=lambda: transport.request(method, plan["resource_id"], body, headers),
        attempt_id=attempt_id)


def matches(plan, body):
    return (body.get("id", "").lower() == plan["resource_id"].lower()
            and body.get("tags", {}).get("distillation-lab-owner") == plan["owner"]
            and body.get("properties", {}).get("model") == plan["body"]["properties"]["model"]
            and all(body.get("sku", {}).get(k) == v for k, v in plan["body"]["sku"].items()))


def deploy(plan_path, *, run_dir, transport=None):
    plan = validate_plan(checked_plan(plan_path, "deployment"))
    transport = transport or ArmTransport()
    common = dict(operation="deploy", input_path=plan_path, run_dir=run_dir, transport=transport)
    previous = _request(plan, "GET", **common)
    if previous["http_status"] != 404:
        raise ValueError("Deployment name must not already exist; no PUT sent")
    response = _request(plan, "PUT", **common, body=plan["body"],
                        headers={"If-None-Match": "*"})
    body = response["body"]
    created_at = body.get("systemData", {}).get("createdAt")
    if response["http_status"] != 201 or not matches(plan, body) or not created_at:
        raise ValueError("Creation/ownership unproven; retain journal and reconcile by GET, never resend")
    payload = {"method": "PUT", "resource_id": plan["resource_id"], "body": plan["body"],
               "headers": {"If-None-Match": "*"}}
    ownership = {"schema_version": 1, "kind": "owned-deployment", "plan": plan,
                 "created_at": created_at, "creation_response": response,
                 "creation_attempt": attempt_key("deploy", payload)}
    write_json(Path(run_dir) / "ownership.json", ownership)
    return ownership


def status(plan_path, *, run_dir, transport=None):
    plan = validate_plan(checked_plan(plan_path, "deployment"))
    return _request(plan, "GET", operation="deploy", input_path=plan_path,
        run_dir=run_dir, transport=transport or ArmTransport(),
        attempt_id="deploy-status-" + uuid.uuid4().hex)


def cleanup(ownership_path, *, run_dir, transport=None):
    ownership = checked_plan(ownership_path, "owned-deployment")
    plan = validate_plan(ownership["plan"])
    creation_id = ownership["creation_attempt"]
    if not re.fullmatch(r"deploy-[0-9a-f]{32}", creation_id):
        raise ValueError("Invalid creation evidence")
    creation = read_json(Path(ownership_path).parent / "attempts" / f"{creation_id}.result.json")
    if (creation.get("status") != "response_received"
            or creation["result"]["response"] != ownership["creation_response"]
            or ownership["creation_response"]["http_status"] != 201):
        raise ValueError("Ownership lacks matching successful creation journal")
    transport = transport or ArmTransport()
    common = dict(operation="cleanup", input_path=ownership_path, run_dir=run_dir, transport=transport)
    current = _request(plan, "GET", **common)
    if (current["http_status"] != 200 or not matches(plan, current["body"])
            or current["body"].get("systemData", {}).get("createdAt") != ownership["created_at"]
            or not current.get("etag")):
        raise ValueError("Ownership or conditional-delete identity cannot be established")
    response = _request(plan, "DELETE", **common, headers={"If-Match": current["etag"]})
    if response["http_status"] not in {200, 202, 204}:
        raise ValueError("Delete was not acknowledged; reconcile by GET without resending")
    write_json(Path(run_dir) / "cleanup-receipt.json", {
        "resource_id": plan["resource_id"], "response": response,
        "state": "deletion_requested_not_verified",
    })
    return response
