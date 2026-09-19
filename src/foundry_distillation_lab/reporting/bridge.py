"""Prepare cost input from saved evaluation evidence, never from invented predictions."""

from collections import Counter
import copy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

from .costs import VARIANTS, _reject_nonfinite, build_report, mapping, number


TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens")
STATUSES = ("technical_failure", "quality_failure", "review_pending", "confirmed_success")
RUNTIMES = ("local_direct_model", "foundry_hosted_agent")


def _row_usage(row):
    usage = row.get("usage")
    return mapping({} if usage is None else usage, "row.usage")


def _runtime(row):
    provenance = row.get("provenance")
    runtime = mapping(provenance, "row.provenance").get("runtime") if provenance is not None else None
    kind = mapping(runtime, "provenance.runtime").get("kind") if runtime is not None else None
    origin = row.get("origin")
    if origin == "local_tool_loop":
        if kind is None:
            return "unknown"
        return "local_direct_model" if kind == "local_direct_model" else "origin_runtime_mismatch"
    if origin == "hosted_capture_import":
        return "foundry_hosted_agent" if kind == "foundry_hosted_agent" else "origin_runtime_mismatch"
    if origin == "synthetic_fixture":
        return "synthetic_fixture"
    return kind if isinstance(kind, str) else "unknown"


def _is_sha256(value):
    return (isinstance(value, str) and len(value) == 64
            and all(character in "0123456789abcdef" for character in value.lower()))


def _bound_review(row):
    review = row.get("review")
    if not isinstance(review, dict) or not _is_sha256(row.get("evidence_sha256")):
        return False
    if review.get("evidence_sha256") != row["evidence_sha256"]:
        return False
    if review.get("decision") not in ("confirmed_success", "quality_failure"):
        return False
    if row.get("text_review") != review["decision"]:
        return False
    if not all(isinstance(review.get(key), str) and review[key].strip()
               for key in ("reviewer", "reviewed_at", "notes")):
        return False
    try:
        return datetime.fromisoformat(review["reviewed_at"].replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _summary(rows, summary):
    """Check saved summaries against rows so partial subtotals cannot become totals."""
    denominator = len(rows)
    if number(summary.get("denominator"), "denominator") != denominator:
        raise ValueError("Evaluation denominator disagrees with scheduled rows")
    statuses = Counter(row.get("status") for row in rows)
    if set(statuses) - set(STATUSES):
        raise ValueError("Unknown evaluation outcome status")
    if summary.get("status_counts") != {key: statuses[key] for key in STATUSES}:
        raise ValueError("Evaluation status_counts disagree with rows")
    for count in summary["status_counts"].values():
        number(count, "status count")
    successes = sum(row.get("confirmed_business_success") is True for row in rows)
    if number(summary.get("confirmed_business_successes"), "confirmed_business_successes") != successes:
        raise ValueError("Evaluation confirmed outcomes disagree with rows")
    means, totals, subtotals, counts = {}, {}, {}, {}
    for key in TOKEN_KEYS:
        values = [number(_row_usage(row).get(key), key)
                  for row in rows]
        known = [value for value in values if value is not None]
        counts[key] = len(known)
        subtotals[key] = sum(known) if known else None
        totals[key] = sum(known) if denominator and len(known) == denominator else None
        for field, expected in (("usage_total", totals[key]),
                                ("usage_known_subtotal", subtotals[key]),
                                ("usage_reported_n", counts[key])):
            observed = mapping(summary.get(field), field).get(key)
            if number(observed, f"{field}.{key}") != expected:
                raise ValueError(f"Evaluation {field}.{key} disagrees with scheduled rows")
        means[key] = totals[key] / denominator if totals[key] is not None else None
    for row in rows:
        usage = _row_usage(row)
        inp, cached = usage.get("input_tokens"), usage.get("cached_input_tokens")
        if inp is not None and cached is not None and cached > inp:
            raise ValueError("Evaluation cached tokens exceed total input tokens")
    return means, {"denominator": denominator, "status_counts": dict(summary["status_counts"]),
                   "usage_total": totals, "usage_known_subtotal": subtotals,
                   "usage_reported_n": counts, "confirmed_business_successes": successes}


def prepare_cost_input(evaluation, config, source_sha256):
    """Use uniform scheduled-slot means; retain missing coverage and hold reasons."""
    mapping(evaluation, "evaluation")
    mapping(config, "config")
    _reject_nonfinite(evaluation)
    if evaluation.get("schema") != "retail-evaluation-report-v1":
        raise ValueError("Expected retail-evaluation-report-v1")
    if evaluation.get("mode") != "e2e":
        raise ValueError("Cost preparation requires mode=e2e; next-action is not a whole customer request")
    assumptions = mapping(config.get("evaluation_projection"), "evaluation_projection")
    if assumptions.get("weighting") != "uniform_scheduled_slots":
        raise ValueError("Only explicit uniform_scheduled_slots weighting is supported")
    expected_runtime = assumptions.get("expected_runtime")
    if expected_runtime not in (*RUNTIMES, "unknown"):
        raise ValueError("expected_runtime must be local_direct_model, foundry_hosted_agent, or unknown")
    if "production_category_mix" not in assumptions:
        raise ValueError("Explicit production_category_mix (or null) is required")
    category_mix = assumptions["production_category_mix"]
    if category_mix is not None:
        mapping(category_mix, "production_category_mix")
        weights = [number(value, "production_category_mix weight") for value in category_mix.values()]
        if not weights or None in weights or not math.isclose(sum(weights), 1.0):
            raise ValueError("production_category_mix weights must sum to one, or use null")
    if not isinstance(assumptions.get("production_equivalence_reviewed"), bool):
        raise ValueError("production_equivalence_reviewed must be explicitly true or false")
    # Validate configured fees, units, and volumes before replacing evaluation-owned fields.
    build_report(config)
    output = copy.deepcopy(config)
    raw_rows = evaluation.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("Evaluation rows must be a nonempty scheduled population")
    per_model = mapping(evaluation.get("per_model"), "per_model")
    if set(per_model) != set(VARIANTS):
        raise ValueError("Combine same-cohort teacher/base/fine_tuned evidence before preparation")
    groups = {name: [] for name in VARIANTS}
    for row in raw_rows:
        mapping(row, "row")
        model = row.get("model")
        if model not in groups or not isinstance(row.get("case_id"), str) or not row["case_id"]:
            raise ValueError("Every row requires a known model and nonempty case_id")
        if not isinstance(row.get("confirmed_business_success"), bool):
            raise ValueError("confirmed_business_success must be boolean")
        if row["confirmed_business_success"] and (
                evaluation["mode"] != "e2e" or row.get("status") != "confirmed_success"):
            raise ValueError("Business success requires an E2E confirmed_success row")
        if evaluation["mode"] == "e2e" and row.get("status") == "confirmed_success" and not row["confirmed_business_success"]:
            raise ValueError("E2E confirmed_success must confirm business success")
        groups[model].append(row)
    if number(evaluation.get("scheduled_slots"), "scheduled_slots") != len(raw_rows):
        raise ValueError("scheduled_slots disagrees with evaluation rows")
    _summary(raw_rows, mapping(evaluation.get("overall"), "overall"))
    cohorts = {name: Counter(row["case_id"] for row in rows) for name, rows in groups.items()}
    same_cohort = all(cohorts[name] == cohorts["teacher"] for name in VARIANTS)
    identity_complete = all(_is_sha256(row.get(key)) for row in raw_rows
                            for key in ("case_sha256", "tool_contract_sha256", "evidence_sha256"))
    identities = {
        name: Counter((row["case_id"],
                       row["case_sha256"] if _is_sha256(row.get("case_sha256")) else "",
                       row["tool_contract_sha256"] if _is_sha256(row.get("tool_contract_sha256")) else "")
                      for row in rows)
        for name, rows in groups.items()}
    same_conditions = identity_complete and all(identities[name] == identities["teacher"] for name in VARIANTS)
    runtime_sets = {name: {_runtime(row) for row in rows} for name, rows in groups.items()}
    runtimes = {name: next(iter(kinds)) if len(kinds) == 1 else "mixed_or_empty"
                for name, kinds in runtime_sets.items()}
    runtime_known = all(value in RUNTIMES for value in runtimes.values())
    runtime_matches = runtime_known and len(set(runtimes.values())) == 1
    runtime_matches = runtime_matches and all(value == expected_runtime for value in runtimes.values())
    reasons = []
    if not same_cohort:
        reasons.append("Model cohorts/case repetition counts differ; no comparable cost crossover.")
    if not identity_complete:
        reasons.append("Case/tool/record hashes are missing or invalid; legacy evidence cannot be promoted.")
    elif not same_conditions:
        reasons.append("Case or tool-contract hashes differ despite case labels; conditions are not comparable.")
    if not runtime_matches:
        reasons.append("Observed runtime provenance is unknown, mixed, or differs from the configured runtime.")
    reasons.append("Uniform evaluation-slot averages are not verified production costs or category-weighted estimates.")
    if not assumptions["production_equivalence_reviewed"]:
        reasons.append("Production equivalence has not been reviewed.")
    if assumptions["production_category_mix"] is None:
        reasons.append("Production category mix is unknown; no category data has been invented.")
    evidence_kind = evaluation.get("evidence_kind", "unknown")
    illustrative = "synthetic" in str(evidence_kind) or "illustrat" in str(evidence_kind)
    illustrative = illustrative or "no_predictions" in str(evidence_kind)
    row_evidence_unknown = False
    for row in raw_rows:
        provenance = row.get("provenance") or {}
        labels = [provenance[key] for key in ("source_evidence_kind", "evidence_kind")
                  if key in provenance]
        if "evidence_kind" in row:
            labels.append(row["evidence_kind"])
        labels = labels or [None]
        for label in labels:
            unknown = not isinstance(label, str) or label in ("", "unknown", "unspecified")
            row_evidence_unknown = row_evidence_unknown or unknown
            illustrative = (illustrative or unknown or "synthetic" in str(label)
                            or "illustrat" in str(label) or "no_predictions" in str(label))
    illustrative = (illustrative or not runtime_known or not identity_complete
                    or evidence_kind in (None, "", "unknown", "unspecified"))
    output["evidence_kind"] = "illustrative" if illustrative else "actual"
    if illustrative:
        reasons.append("Illustrative or insufficiently identified evaluation evidence cannot become measured production evidence.")
    if row_evidence_unknown:
        reasons.append("At least one row has unknown evidence provenance; a root label cannot promote it.")
    model_audit = {}
    for name, rows in groups.items():
        means, audit = _summary(rows, mapping(per_model[name], name))
        if not rows:
            reasons.append(f"{name}: empty scheduled cohort.")
        if any(value is None for value in means.values()):
            reasons.append(f"{name}: incomplete usage; known subtotal is not a total.")
        reviewed = [
            row for row in rows if _bound_review(row) and (
                (row.get("status") == "confirmed_success"
                 and row.get("confirmed_business_success") is True
                 and row.get("deterministic_checks_passed") is True
                 and row.get("text_review") == "confirmed_success")
                or (row.get("status") == "quality_failure"
                    and row.get("text_review") == "quality_failure"))]
        complete = bool(rows) and len(reviewed) == len(rows)
        successes = sum(row["confirmed_business_success"] for row in reviewed) if complete else None
        if not complete:
            reasons.append(f"{name}: unknown/unreviewed outcomes; projected success cost is undefined.")
        variant = output["variants"][name]
        if variant["inference_mode"] != "tokens":
            raise ValueError("Evaluation preparation requires token prices, not per_request estimates")
        variant["usage_per_request"] = means
        gate = variant.get("quality", {}).get("quality_gate", "unreviewed")
        variant["quality"] = {"review_complete": complete, "reviewed_requests": len(reviewed),
                              "successful_requests": successes, "quality_gate": gate}
        source = variant.setdefault("source", {})
        mapping(source, "source")
        source.update({"usage_artifact": f"sha256:{source_sha256}",
                       "quality_artifact": f"sha256:{source_sha256}",
                       "evaluation_source_sha256": source_sha256,
                       "evaluation_runtime": runtimes[name],
                       "evaluation_evidence_kind": evidence_kind,
                       "evaluation_scope": evaluation["mode"],
                       "observed_category_mix": None,
                       "projection_denominator": len(rows),
                       "projection_weighting": "uniform_scheduled_slots"})
        audit.update({"cohort": dict(sorted(cohorts[name].items())),
                      "reviewed_final_outcomes": len(reviewed), "runtime": runtimes[name],
                      "runtime_kinds": sorted(runtime_sets[name]),
                      "row_provenance": [{"case_id": row["case_id"],
                                          "category": row.get("category"),
                                          "origin": row.get("origin"),
                                          "case_sha256": row.get("case_sha256"),
                                          "tool_contract_sha256": row.get("tool_contract_sha256"),
                                          "evidence_sha256": row.get("evidence_sha256"),
                                          "review": copy.deepcopy(row.get("review")),
                                          "provenance": copy.deepcopy(row.get("provenance"))}
                                         for row in rows]})
        model_audit[name] = audit
    output["evaluation_bridge"] = {
        "schema": "evaluation-cost-bridge-v1", "source_sha256": source_sha256,
        "source_schema": evaluation["schema"], "source_evidence_kind": evidence_kind,
        "evaluation_mode": evaluation["mode"], "same_cohort": same_cohort,
        "identity_complete": identity_complete, "same_conditions": same_conditions,
        "runtime_matches": runtime_matches,
        "comparable": same_cohort and same_conditions and runtime_matches,
        "production_evidence": False, "observed_category_mix": None,
        "assumptions": copy.deepcopy(assumptions), "per_model": model_audit,
        "hold_reasons": reasons,
    }
    build_report(output)
    return output


def prepare_from_files(evaluation_path, config_path, output_path):
    raw = Path(evaluation_path).read_bytes()
    config_raw = Path(config_path).read_bytes()
    prepared = prepare_cost_input(
        json.loads(raw.decode("utf-8-sig")),
        json.loads(config_raw.decode("utf-8-sig")), hashlib.sha256(raw).hexdigest())
    prepared["evaluation_bridge"]["config_sha256"] = hashlib.sha256(config_raw).hexdigest()
    serialized = json.dumps(prepared, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(serialized)
    return prepared
