"""Offline evidence checks. Passing automatic checks is not business success."""

from collections import Counter
from copy import deepcopy
import hashlib
import math
import statistics

from .schema import ContractError, call_errors, canonical, equal, parse_call, tool_schemas, validate


DEFAULT_MODELS = ("teacher", "base", "fine_tuned")
STATUSES = ("technical_failure", "quality_failure", "review_pending", "confirmed_success")
MUTATION_TOOLS = {"submit_resolution"}


def evidence_sha256(record):
    """Human and model reviews independently bind to the same raw evidence."""
    evidence = {key: value for key, value in record.items() if key not in ("review", "model_review")}
    return hashlib.sha256(canonical(evidence).encode("utf-8")).hexdigest()


def _evidence_digest(record):
    if not isinstance(record, dict):
        return None
    try:
        return evidence_sha256(record)
    except (ValueError, TypeError):
        return None


def _review(record):
    review = record.get("review")
    if not isinstance(review, dict):
        return None
    if (not isinstance(review.get("decision"), str)
            or review["decision"] not in {"confirmed_success", "quality_failure"}
            or not all(isinstance(review.get(key), str) and review[key].strip()
                       for key in ("reviewer", "reviewed_at", "notes"))
            or _evidence_digest(record) is None
            or review.get("evidence_sha256") != _evidence_digest(record)):
        return None
    return review["decision"]


def normalized_usage(value):
    keys = ("input_tokens", "output_tokens", "cached_input_tokens")
    result = {key: None for key in keys}
    if not isinstance(value, dict):
        return result
    for key in keys:
        if type(value.get(key)) is int and value[key] >= 0:
            result[key] = value[key]
    if (result["cached_input_tokens"] is not None and
            (result["input_tokens"] is None or result["cached_input_tokens"] > result["input_tokens"])):
        result["cached_input_tokens"] = None
    return result


def aggregate_usage(values):
    normalized = [normalized_usage(value) for value in values]
    return {
        key: sum(value[key] for value in normalized)
        if normalized and all(value[key] is not None for value in normalized) else None
        for key in ("input_tokens", "output_tokens", "cached_input_tokens")
    }


def _base(case, record, model):
    return {
        "case_id": case["case_id"], "model": model,
        "category": case.get("category"),
        "case_sha256": hashlib.sha256(canonical(case).encode("utf-8")).hexdigest(),
        "evidence_sha256": _evidence_digest(record),
        "review": deepcopy(record.get("review")) if isinstance(record, dict) else None,
        "status": "technical_failure", "confirmed_business_success": False,
        "deterministic_checks_passed": False, "technical_failures": [], "quality_failures": [],
        "blocked_invalid_calls": [], "executed_unauthorized_actions": [],
        "text_review": "unreviewed",
        "provenance": deepcopy(record.get("provenance")) if isinstance(record, dict) else None,
        "origin": record.get("origin") if isinstance(record, dict) else None,
        "source_review": deepcopy(record.get("source_review")) if isinstance(record, dict) else None,
        "usage": normalized_usage(record.get("usage")) if isinstance(record, dict) else normalized_usage(None),
        "latency_seconds": None,
    }


def _record_errors(case, record, model, score):
    if not isinstance(record, dict):
        return ["missing_record"] if record is None else ["record_not_object"]
    errors = []
    if record.get("case_id") != case["case_id"] or record.get("model") != model:
        errors.append("record_identity_mismatch")
    if record.get("status") != "completed":
        errors.append("attempt_not_completed")
    latency = record.get("latency_seconds")
    if type(latency) in (int, float) and math.isfinite(latency) and latency >= 0:
        score["latency_seconds"] = latency
    return errors


def _finish(score, record):
    technical, quality = score["technical_failures"], score["quality_failures"]
    score["deterministic_checks_passed"] = not technical and not quality
    review = _review(record) if isinstance(record, dict) else None
    if review:
        score["text_review"] = review
    score["status"] = ("technical_failure" if technical else "quality_failure"
                       if quality or review == "quality_failure" else "confirmed_success"
                       if review == "confirmed_success" else "review_pending")
    score["confirmed_business_success"] = (
        score.get("evaluation_scope") == "e2e" and score["status"] == "confirmed_success")
    score["human_review_required"] = review is None
    from .grading import validated_review
    automatic = validated_review(record, score)
    score["model_review"] = automatic
    has_automatic = isinstance(record, dict) and "model_review" in record
    score["automatic_decision"] = (
        "unknown" if technical else "failure" if quality else
        automatic["decision"] if automatic else "unknown")
    score["assessment_source"] = (
        "deterministic" if technical or quality else "model" if automatic else
        "invalid_model_review" if has_automatic else "human" if review else "unreviewed")
    return score


def score_next_action(case, record, tools, model):
    schemas = tool_schemas(tools)
    expected = case.get("expected")
    if (not isinstance(expected, dict) or not isinstance(expected.get("kind"), str)
            or expected["kind"] not in {"tool", "text"}):
        raise ContractError("next_action_expected_kind_required")
    reference = expected.get("calls")
    if not isinstance(reference, list):
        raise ContractError("expected_calls_required")
    try:
        reference = [parse_call(call) for call in reference]
    except (ValueError, TypeError) as exc:
        raise ContractError("invalid_reference_calls") from exc
    if (any(call_errors(call, schemas) for call in reference)
            or (expected["kind"] == "tool") != bool(reference)):
        raise ContractError("reference_calls_violate_contract")
    score = _base(case, record, model)
    score.update(evaluation_scope="next-action", strict_tool_match=False,
                 schema_valid=False, predicted_call_count=None)
    score["tool_contract_sha256"] = hashlib.sha256(canonical(tools).encode("utf-8")).hexdigest()
    score["technical_failures"] = _record_errors(case, record, model, score)
    if not isinstance(record, dict):
        return _finish(score, record)
    message = record.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("tool_calls", []), list):
        score["technical_failures"].append("malformed_message")
        return _finish(score, record)
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        score["technical_failures"].append("malformed_content")
    calls = message.get("tool_calls", [])
    score["predicted_call_count"] = len(calls)
    try:
        calls = [parse_call(call) for call in calls]
    except (ValueError, TypeError) as exc:
        score["quality_failures"].append("malformed_tool_arguments")
        return _finish(score, record)
    errors = [error for call in calls for error in call_errors(call, schemas)]
    score["schema_valid"] = not errors
    if errors:
        score["quality_failures"].extend(errors)
    # Multiplicity matters, but parallel call order is not a next-action metric.
    score["strict_tool_match"] = (
        not errors and Counter(canonical(call) for call in calls) == Counter(canonical(call) for call in reference))
    if not score["strict_tool_match"]:
        score["quality_failures"].append("strict_tool_mismatch")
    if expected["kind"] == "text" and (not isinstance(content, str) or not content.strip()):
        score["quality_failures"].append("missing_text")
    return _finish(score, record)


def _value_matches(actual, expected, subset=False):
    if subset and isinstance(expected, dict):
        return (isinstance(actual, dict) and all(
            key in actual and _value_matches(actual[key], value, True) for key, value in expected.items()))
    if subset and isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(_value_matches(left, right, True) for left, right in zip(actual, expected)))
    return equal(actual, expected)


def _matches(event, expected):
    subset = expected.get("match", "exact") == "subset"
    return (event.get("name") == expected["name"]
            and _value_matches(event.get("arguments"), expected["arguments"], subset)
            and ("result" not in expected or _value_matches(event.get("result"), expected["result"], subset)))


def _partial_schema(schema):
    result = dict(schema)
    if schema["type"] == "object":
        result["required"] = []
        result["properties"] = {key: _partial_schema(value) for key, value in schema.get("properties", {}).items()}
    if schema["type"] == "array":
        result["items"] = _partial_schema(schema["items"])
    return result


def _expectations(case, schemas):
    expected = case.get("expected")
    required = {"initial_tools", "required_calls", "allowed_mutations", "final_state"}
    if not isinstance(expected, dict) or not required.issubset(expected):
        raise ContractError("explicit_e2e_expectations_required")
    initial = expected["initial_tools"]
    if not isinstance(initial, list) or any(not isinstance(name, str) or name not in schemas for name in initial):
        raise ContractError("invalid_initial_tools")
    for key in ("required_calls", "allowed_mutations"):
        if not isinstance(expected[key], list):
            raise ContractError(f"invalid_{key}")
        for entry in expected[key]:
            try:
                if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                        or not isinstance(entry.get("arguments"), dict)
                        or ("result" in entry and not isinstance(entry["result"], dict))):
                    raise ValueError("normalized_expected_call_required")
                call = parse_call(entry)
                if entry.get("match", "exact") not in ("exact", "subset"):
                    raise ValueError("unknown_expectation_match")
                if call["name"] not in schemas:
                    raise ValueError("unknown_expected_tool")
                schema = schemas[call["name"]]
                if entry.get("match") == "subset":
                    schema = _partial_schema(schema)
                if validate(call["arguments"], schema):
                    raise ValueError("expected_call_schema")
                if key == "allowed_mutations" and (call["name"] not in MUTATION_TOOLS or "result" not in entry):
                    raise ValueError("authorized_mutation_needs_exact_result")
                if key == "allowed_mutations" and entry.get("match") == "subset":
                    args, result = call["arguments"], entry["result"]
                    if (not all(isinstance(args.get(field), str) and args[field]
                                for field in ("order_id", "calculation_id"))
                            or not isinstance(result, dict)
                            or any(result.get(field) != args[field] for field in ("order_id", "calculation_id"))
                            or result.get("external_side_effect") is not False
                            or result.get("status") != "処理シミュレーション完了"):
                        raise ValueError("subset_mutation_requires_explicit_authority_and_simulation")
            except (ValueError, TypeError) as exc:
                raise ContractError(f"invalid_{key}") from exc
    if (not isinstance(expected["final_state"], dict)
            or set(expected["final_state"]) != {"terminal", "submissions"}
            or expected["final_state"]["terminal"] not in ("answer_only", "simulation_submitted")
            or not isinstance(expected["final_state"]["submissions"], list)):
        raise ContractError("invalid_expected_final_state")
    if expected.get("final_state_match", "exact") not in ("exact", "subset"):
        raise ContractError("invalid_final_state_match")
    return expected


def derive_final_state(successful):
    submissions = [event["result"] for event in successful if event["name"] in MUTATION_TOOLS]
    return {"terminal": "simulation_submitted" if submissions else "answer_only",
            "submissions": submissions}


def score_e2e(case, record, tools, model):
    schemas = tool_schemas(tools)
    expected = _expectations(case, schemas)
    score = _base(case, record, model)
    score["evaluation_scope"] = "e2e"
    score["tool_contract_sha256"] = hashlib.sha256(canonical(tools).encode("utf-8")).hexdigest()
    score["usage"] = normalized_usage(None)
    technical, quality = score["technical_failures"], score["quality_failures"]
    technical.extend(_record_errors(case, record, model, score))
    if not isinstance(record, dict):
        return _finish(score, record)
    events = record.get("events")
    if not isinstance(events, list) or not events:
        technical.append("missing_event_evidence")
        return _finish(score, record)
    starts, finished, successful, model_starts, model_finishes = {}, set(), [], set(), set()
    proposals, blocked_ids, terminal_message = {}, set(), None
    completion_count, model_usages = 0, []
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            technical.append("malformed_event")
            continue
        kind, call_id = event["event"], event.get("call_id")
        framework_identity_verified = (event.get("framework_call_id_verified") is True
                                       or event.get("framework_context_call_id_available") is True)
        if kind in ("tool_start", "tool_finish") and (
                event.get("framework_call_id_verified") is False
                or event.get("framework_context_call_id_available") is False
                or event.get("call_id_source") in (
                    "unique_unconsumed_observed_proposal", "unique_observed_proposal_match")
                or (record.get("origin") == "hosted_capture_import" and not framework_identity_verified)):
            technical.append("unverified_framework_tool_call_linkage")
            call_id = None
        if kind in {"model_start", "model_finish", "tool_start", "tool_finish", "tool_blocked"}:
            if not isinstance(call_id, str) or not call_id:
                technical.append("missing_call_id")
                continue
        if kind == "model_start":
            if call_id in model_starts:
                technical.append("duplicate_model_start")
            if model_starts != model_finishes or set(starts) != finished:
                technical.append("overlapping_or_unfinished_calls")
            if set(proposals) != set(starts) | blocked_ids:
                technical.append("model_started_before_tools_finished")
            model_starts.add(call_id)
        elif kind == "model_finish":
            model_usages.append(event.get("usage"))
            if call_id not in model_starts or call_id in model_finishes:
                technical.append("unpaired_model_finish")
            model_finishes.add(call_id)
            if event.get("status") != "completed":
                technical.append("model_failed")
                continue
            message = event.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("tool_calls", []), list):
                technical.append("missing_model_message")
                continue
            terminal_message = message
            for proposal in message.get("tool_calls", []):
                if not isinstance(proposal, dict) or not isinstance(proposal.get("id"), str):
                    technical.append("malformed_model_tool_proposal")
                    continue
                proposal_id = proposal["id"]
                if not proposal_id or proposal_id in proposals:
                    technical.append("duplicate_or_empty_proposal_id")
                try:
                    proposals[proposal_id] = parse_call(proposal)
                except (ValueError, TypeError):
                    proposals[proposal_id] = None
                    quality.append("malformed_model_tool_proposal")
        elif kind == "tool_start":
            if call_id in starts:
                technical.append("duplicate_tool_start")
            if len(starts) < len(expected["initial_tools"]) and set(starts) != finished:
                quality.append("initial_checks_not_completed_in_order")
            starts[call_id] = event
            try:
                parsed = parse_call(event)
                if call_id not in proposals or not equal(parsed, proposals[call_id]):
                    technical.append("tool_start_not_supported_by_model")
                if call_errors(parsed, schemas):
                    quality.append("executed_schema_invalid_call")
            except (ValueError, TypeError):
                technical.append("malformed_tool_start")
        elif kind == "tool_blocked":
            if call_id in starts:
                technical.append("blocked_call_already_started")
            score["blocked_invalid_calls"].append(event)
            blocked_ids.add(call_id)
            quality.append("blocked_invalid_call")
        elif kind == "tool_finish":
            if call_id not in starts or call_id in finished:
                technical.append("unpaired_tool_finish")
                continue
            finished.add(call_id)
            start = starts[call_id]
            raw_result = event.get("result")
            if isinstance(raw_result, dict) and raw_result.get("external_side_effect") is True:
                quality.append("real_system_execution")
                if event.get("status") != "completed" and start.get("name") in MUTATION_TOOLS:
                    score["executed_unauthorized_actions"].append({
                        "name": start["name"], "arguments": start.get("arguments"), "result": raw_result})
            if event.get("status") == "blocked":
                if not isinstance(event.get("result"), dict) or event["result"].get("external_side_effect") is not False:
                    technical.append("blocked_side_effect_not_verified")
                score["blocked_invalid_calls"].append({**start, "result": event.get("result")})
                quality.append("blocked_invalid_call")
                continue
            if event.get("status") != "completed":
                technical.append("tool_error_or_unknown_outcome")
                continue
            result = event.get("result")
            if not isinstance(result, dict):
                technical.append("malformed_tool_result")
                continue
            if result.get("error") or result.get("status") in ("error", "failed", "blocked", "rejected"):
                technical.append("error_result_marked_completed")
                continue
            try:
                parsed = parse_call(start)
            except (ValueError, TypeError):
                continue
            evidence = {**parsed, "result": result}
            successful.append(evidence)
            if evidence["name"] in MUTATION_TOOLS:
                if not any(_matches(evidence, allowed) for allowed in expected["allowed_mutations"]):
                    score["executed_unauthorized_actions"].append(evidence)
                    quality.append("executed_unauthorized_action")
                if result.get("external_side_effect") is not False:
                    quality.append("simulation_boundary_unverified_or_violated")
        elif kind == "attempt_finish":
            completion_count += 1
            if event.get("status") != "completed":
                technical.append("attempt_finish_not_completed")
        else:
            technical.append("unknown_event_type")
    if not model_starts or model_starts != model_finishes:
        technical.append("incomplete_model_evidence")
        model_usages.append(None)
    score["usage"] = aggregate_usage(model_usages)
    if set(starts) != finished:
        technical.append("incomplete_tool_evidence")
    if (completion_count != 1 or not isinstance(events[-1], dict)
            or events[-1].get("event") != "attempt_finish"):
        technical.append("missing_or_misplaced_attempt_finish")
    if set(proposals) != set(starts) | blocked_ids:
        technical.append("unaccounted_model_tool_proposals")
    if (not isinstance(terminal_message, dict) or terminal_message.get("tool_calls")
            or terminal_message.get("content") != record.get("answer")):
        technical.append("final_answer_not_supported_by_model")
    initial_names = [event.get("name") for event in starts.values()][:len(expected["initial_tools"])]
    if initial_names != expected["initial_tools"]:
        quality.append("missing_required_initial_order")
    remaining = successful.copy()
    for required in expected["required_calls"]:
        match = next((index for index, item in enumerate(remaining) if _matches(item, required)), None)
        if match is None:
            quality.append("missing_required_call_or_result")
        else:
            remaining.pop(match)
    derived = derive_final_state(successful)
    if not equal(record.get("final_state"), derived):
        technical.append("final_state_not_supported_by_events")
    if not _value_matches(derived, expected["final_state"], expected.get("final_state_match") == "subset"):
        quality.append("unexpected_final_state")
    if not isinstance(record.get("answer"), str) or not record["answer"].strip():
        quality.append("missing_final_answer")
    score["technical_failures"] = sorted(set(technical))
    score["quality_failures"] = sorted(set(quality))
    return _finish(score, record)


def _runtime_kind(row):
    if row.get("evidence_sha256") is None:
        return "unknown"
    if (row.get("origin") == "synthetic_fixture"
            or row.get("evidence_kind") == "synthetic_illustration_not_measurement"):
        return "synthetic"
    provenance = row.get("provenance")
    runtime = provenance.get("runtime") if isinstance(provenance, dict) else None
    if (row.get("origin") in ("local_tool_loop", "local_next_action")
            and isinstance(runtime, dict) and runtime.get("kind") == "local_direct_model"):
        return "local_direct_model"
    if (row.get("origin") == "hosted_capture_import" and isinstance(runtime, dict)
            and runtime.get("kind") == "foundry_hosted_agent"):
        return "hosted_capture"
    return "unknown"


def _summary(rows):
    counts = {status: sum(row["status"] == status for row in rows) for status in STATUSES}
    latencies = sorted(row["latency_seconds"] for row in rows if row["latency_seconds"] is not None)
    known = [row["usage"] for row in rows]
    runtime_counts = Counter(_runtime_kind(row) for row in rows)
    runtime_kinds = set(runtime_counts)
    return {
        "denominator": len(rows), "status_counts": counts,
        "confirmed_success_rate": counts["confirmed_success"] / len(rows) if rows else None,
        "confirmed_business_successes": sum(row["confirmed_business_success"] for row in rows),
        "automatic_check_passes": sum(row["deterministic_checks_passed"] for row in rows),
        "automatic_decision_counts": {
            decision: sum(row["automatic_decision"] == decision for row in rows)
            for decision in ("success", "failure", "needs_review", "unknown")},
        "assessment_source_counts": dict(Counter(row["assessment_source"] for row in rows)),
        "runtime_provenance": {
            "classification": next(iter(runtime_kinds)) if len(runtime_kinds) == 1 else
                              "mixed" if runtime_kinds else "unknown",
            "counts": dict(sorted(runtime_counts.items())),
            "basis": "row_origin_and_captured_metadata_not_remote_attestation",
        },
        "latency_seconds": {
            "population": "records_with_reported_latency_including_failures",
            "n": len(latencies), "median": statistics.median(latencies) if latencies else None,
            "p95": latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else None,
            "p95_method": "nearest_rank",
        },
        "usage_total": aggregate_usage(known),
        "usage_known_subtotal": {
            key: sum(value[key] for value in known if value[key] is not None)
            if any(value[key] is not None for value in known) else None
            for key in ("input_tokens", "output_tokens", "cached_input_tokens")
        },
        "usage_reported_n": {
            key: sum(value[key] is not None for value in known)
            for key in ("input_tokens", "output_tokens", "cached_input_tokens")
        },
    }


def evaluate(bundle, mode):
    if mode not in {"next-action", "e2e"} or not isinstance(bundle, dict):
        raise ContractError("invalid_evaluation_mode_or_bundle")
    models = bundle.get("models", list(DEFAULT_MODELS))
    cases, records = bundle.get("cases"), bundle.get("records", [])
    if (not isinstance(models, list) or not models
            or any(not isinstance(model, str) or not model for model in models)
            or len(set(models)) != len(models)):
        raise ContractError("unique_model_labels_required")
    if not isinstance(cases, list) or not cases or not isinstance(records, list):
        raise ContractError("cases_and_records_must_be_lists")
    case_ids = []
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str) or not case["case_id"]:
            raise ContractError("case_id_required")
        case_ids.append(case["case_id"])
    if len(set(case_ids)) != len(case_ids):
        raise ContractError("duplicate_case_id")
    index = {}
    for record in records:
        if (not isinstance(record, dict) or record.get("case_id") not in case_ids
                or record.get("model") not in models):
            raise ContractError("record_outside_declared_evaluation")
        key = (record["case_id"], record["model"])
        if key in index:
            raise ContractError("duplicate_case_model_record")
        index[key] = record
    scorer = score_next_action if mode == "next-action" else score_e2e
    rows = [scorer(case, index.get((case["case_id"], model)),
                   case.get("tools", bundle.get("tools")), model)
            for case in cases for model in models]
    for row in rows:
        provenance = row.get("provenance")
        row["evidence_kind"] = (provenance.get("evidence_kind", bundle.get("evidence_kind", "unspecified"))
                                if isinstance(provenance, dict) else bundle.get("evidence_kind", "unspecified"))
    return {
        "schema": "retail-evaluation-report-v1", "mode": mode,
        "evidence_kind": bundle.get("evidence_kind", "unspecified"),
        "imports": deepcopy(bundle.get("imports", [])),
        "business_success_requires_human_review": True,
        "scheduled_slots": len(cases) * len(models), "observed_records": len(records),
        "rows": rows, "overall": _summary(rows),
        "per_model": {model: _summary([row for row in rows if row["model"] == model]) for model in models},
        "per_case": {case_id: _summary([row for row in rows if row["case_id"] == case_id]) for case_id in case_ids},
    }


def prepare_next_actions(rows, source_sha256):
    """Adapt development next-actions JSONL without inventing predictions."""
    if not isinstance(rows, list) or not rows:
        raise ContractError("nonempty_next_actions_required")
    cases = []
    for row in rows:
        if (not isinstance(row, dict) or row.get("kind") not in ("tool", "text")
                or not isinstance(row.get("messages"), list) or not row["messages"]
                or not isinstance(row.get("ground_truth"), list)
                or not isinstance(row.get("reference"), dict)):
            raise ContractError("invalid_prepared_next_action")
        if not equal(row["reference"].get("tool_calls", []), row["ground_truth"]):
            raise ContractError("prepared_reference_ground_truth_mismatch")
        case = deepcopy(row)
        case["expected"] = {"kind": row["kind"], "calls": deepcopy(row["ground_truth"])}
        cases.append(case)
    bundle = {
        "schema": "retail-evaluation-input-v1",
        "evidence_kind": "unreviewed_cases_no_predictions",
        "source": {"kind": "development_next_actions_jsonl", "sha256": source_sha256,
                   "quality_review_approved": False},
        "models": list(DEFAULT_MODELS), "cases": cases, "records": [],
    }
    evaluate(bundle, "next-action")
    return bundle
