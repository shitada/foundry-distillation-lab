"""Offline scoring by default; explicit, approved direct-model inference only."""

import argparse
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from foundry_distillation_lab.evaluation import (  # noqa: E402
    DEFAULT_MODELS, GuardedModel, OpenAITransport, evaluate, import_hosted,
    prepare_next_actions, run_case, run_next_action,
)
from foundry_distillation_lab.evaluation.schema import ContractError, loads  # noqa: E402
from foundry_distillation_lab.io import read_jsonl, sha256, write_json as write_new  # noqa: E402


def live(bundle, args, input_digest):
    if not args.approval or not args.run_dir:
        raise ContractError("--send_requires_--approval_and_--run-dir")
    if bundle.get("records"):
        raise ContractError("live_input_must_not_contain_saved_records")
    config = bundle.get("inference")
    if not isinstance(config, dict):
        raise ContractError("approved_input_requires_inference_configuration")
    targets = config.get("targets")
    labels = bundle.get("models", list(DEFAULT_MODELS))
    if (not isinstance(targets, dict) or set(targets) != set(labels)
            or any(not isinstance(target, str) or not target for target in targets.values())):
        raise ContractError("one_explicit_target_per_model_required")
    cost = config.get("reserve_per_request")
    if type(cost) not in (int, float) or not math.isfinite(cost) or cost <= 0:
        raise ContractError("positive_conservative_reservation_required")
    max_model_calls = config.get("max_model_calls", 12)
    max_tool_calls = config.get("max_tool_calls", 24)
    if any(type(limit) is not int or limit <= 0 for limit in (max_model_calls, max_tool_calls)):
        raise ContractError("positive_integer_loop_limits_required")
    from foundry_distillation_lab.safety import Approval, Journal
    approval = Approval.load(args.approval, operation=f"eval-{args.mode}", input_path=args.input)
    if approval.data["input_sha256"] != input_digest:
        raise ContractError("approved_file_changed_since_payload_was_read")
    for target in targets.values():
        approval.assert_target(target)
    worst_requests = len(bundle["cases"]) * len(labels) * (max_model_calls if args.mode == "e2e" else 1)
    if worst_requests > approval.max_requests or worst_requests * cost > approval.max_cost:
        raise ContractError("worst_case_requests_or_reservations_exceed_approval")
    transport = OpenAITransport(
        base_url=config["base_url"],
        max_completion_tokens=config.get("max_completion_tokens", 2048),
        timeout_seconds=config.get("timeout_seconds", 60),
    )
    if args.mode == "e2e":
        from foundry_distillation_lab.retail import RetailSession
        # Bind the system prompt as well as the tool contract to the approved file.
        for case in bundle["cases"]:
            if not isinstance(case.get("user_input"), str) or not case["user_input"].strip():
                raise ContractError("live_e2e_requires_user_input")
            if not isinstance(case.get("system_prompt"), str) or not case["system_prompt"]:
                raise ContractError("live_e2e_requires_approved_system_prompt_per_case")
            probe = RetailSession()
            if probe.system_prompt != case["system_prompt"] or probe.tools != case.get("tools", bundle.get("tools")):
                raise ContractError("retail_contract_differs_from_approved_input")
    elif any(not isinstance(case.get("messages"), list) or not case["messages"]
             for case in bundle["cases"]):
        raise ContractError("live_next_action_requires_messages")
    args.run_dir.mkdir(parents=True, exist_ok=False)
    write_new(args.run_dir / "input.json", bundle)
    journal = Journal(args.run_dir)
    evidence = deepcopy(bundle)
    evidence["records"] = []
    stop = False
    for case in bundle["cases"]:
        for label in labels:
            invoke = GuardedModel(transport, approval=approval, journal=journal, target=targets[label],
                                  reserve_per_request=cost, send=True)
            tools = case.get("tools", bundle.get("tools"))
            if args.mode == "next-action":
                record = run_next_action(case, label, invoke, tools)
            else:
                record = run_case(case, label, invoke, RetailSession, max_model_calls=max_model_calls,
                                  max_tool_calls=max_tool_calls, tools=tools)
            record["provenance"] = {
                "runtime": {
                    "kind": "local_direct_model", "model": targets[label],
                    "endpoint": config["base_url"],
                    "model_identity_basis": "approved_deployment_target_not_resolved_model_version",
                    "latency_scope": "local_wall_clock_model_and_tool_loop",
                },
                "approved_input_sha256": input_digest,
                "evidence_kind": bundle.get("evidence_kind", "unspecified"),
                "identity_assurance": "approved_target_and_local_runner_not_hosted_observation",
            }
            evidence["records"].append(record)
            write_new(args.run_dir / f"record-{len(evidence['records']):06d}.json", record)
            if record["status"] != "completed":
                stop = True
                break
        if stop:
            break
    write_new(args.run_dir / "evidence.json", evidence)
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("next-action", "e2e"))
    parser.add_argument("--input", required=True, type=Path, help="UTF-8 JSON cases/tools/saved-record bundle")
    parser.add_argument("--output", required=True, type=Path, help="New JSON score report; never overwritten")
    parser.add_argument("--send", action="store_true", help="Enable approved LOCAL direct-model requests (not Hosted Agent)")
    parser.add_argument("--approval", type=Path, help="Approval bound to operation, targets, input hash, deadline, budget")
    parser.add_argument("--run-dir", type=Path, help="New directory for immutable model request journals and evidence")
    parser.add_argument("--prepare-next-actions", action="store_true",
                        help="Offline: convert prepared development JSONL into an empty prediction bundle, not a score")
    parser.add_argument("--import-hosted", type=Path,
                        help="Offline: import Hosted execution-evidence JSON object/list or JSONL into --input E2E bundle")
    parser.add_argument("--model-label", help="Declared model label for --import-hosted only")
    args = parser.parse_args(argv)
    try:
        if args.prepare_next_actions:
            if (args.mode != "next-action" or args.send or args.approval or args.run_dir
                    or args.import_hosted or args.model_label):
                raise ContractError("preparation_requires_next-action_mode_without_live_flags")
            bundle = prepare_next_actions(read_jsonl(args.input), sha256(args.input))
            write_new(args.output, bundle)
            print(f"prepared {len(bundle['cases'])} cases; no predictions or scores: {args.output}")
            return 0
        input_bytes = args.input.read_bytes()
        bundle = loads(input_bytes.decode("utf-8-sig"))
        if args.import_hosted:
            if args.mode != "e2e" or args.send or args.approval or args.run_dir or not args.model_label:
                raise ContractError("hosted_import_requires_e2e_model_label_without_live_flags")
            capture_bytes = args.import_hosted.read_bytes()
            text = capture_bytes.decode("utf-8-sig")
            try:
                captures = loads(text)
            except ValueError:
                captures = [loads(line) for line in text.splitlines() if line.strip()]
            if isinstance(captures, dict):
                captures = [captures]
            imported = import_hosted(bundle, captures, args.model_label,
                                     hashlib.sha256(capture_bytes).hexdigest())
            write_new(args.output, imported)
            print(f"imported {len(captures)} Hosted evidence records offline; {args.output}")
            return 0
        if args.model_label:
            raise ContractError("--model-label_requires_--import-hosted")
        # Validate every expectation before any potential network call.
        report = evaluate(bundle, args.mode)
        if not args.send and (args.approval or args.run_dir):
            raise ContractError("--approval_and_--run-dir_require_--send")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Reserve the output path before paid work; a crash leaves a non-reusable file.
        with args.output.open("x", encoding="utf-8") as output:
            if args.send:
                report = evaluate(live(bundle, args, hashlib.sha256(input_bytes).hexdigest()), args.mode)
            json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
        print(f"{args.mode}: {report['observed_records']}/{report['scheduled_slots']} records; {args.output}")
        return 0
    except (OSError, ValueError, KeyError, ImportError, PermissionError) as exc:
        print(f"evaluation stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
