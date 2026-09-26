"""Create-only direct evaluation runs and offline review/comparison/combined artifacts."""

from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from ..io import read_json, sha256, write_json
from ..safety import Journal
from .runner import OpenAITransport, RecordedModel, run_case, run_next_action
from .schema import ContractError, call_errors, canonical, loads, parse_call, tool_schemas
from .scoring import DEFAULT_MODELS, evaluate, evidence_sha256, score_e2e, score_next_action


REVIEW_FIELDS = ("case_id", "model", "category", "evidence_sha256", "messages", "expected", "reference", "response",
                 "status", "reasons", "latency_seconds", "usage", "decision", "reviewer", "notes")
USER_REVIEW_FIELDS = {"decision", "reviewer", "notes"}
CONFIG_FIELDS = {"base_url", "targets", "max_completion_tokens", "timeout_seconds",
                 "max_model_calls", "max_tool_calls"}
LIVE_EVIDENCE_KIND = "live_local_direct_model"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _messages(messages, tools):
    """Validate the supported text/function conversation before any requests."""
    if not isinstance(messages, list) or not messages:
        raise ContractError("next_action_messages_required")
    schemas = tool_schemas(tools)
    pending, seen = set(), set()
    for message in messages:
        if not isinstance(message, dict):
            raise ContractError("input_message_must_be_object")
        role = message.get("role")
        if role not in ("system", "developer", "user", "assistant", "tool"):
            raise ContractError("invalid_input_message_role")
        allowed = {"role", "content", "name"}
        if role == "assistant":
            allowed.add("tool_calls")
        if role == "tool":
            allowed.add("tool_call_id")
        if set(message) - allowed:
            raise ContractError("unsupported_input_message_fields")
        if "name" in message and (not isinstance(message["name"], str) or not message["name"].strip()):
            raise ContractError("invalid_input_message_name")
        content = message.get("content")
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list):
            raise ContractError("input_tool_calls_must_be_list")
        if not isinstance(content, str) and not (role == "assistant" and calls and content is None):
            raise ContractError("input_message_requires_text_or_assistant_calls")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise ContractError("unmatched_input_tool_result")
            pending.remove(call_id)
        elif pending:
            raise ContractError("missing_input_tool_results")
        for call in calls:
            if (not isinstance(call, dict) or call.get("type") != "function"
                    or not isinstance(call.get("function"), dict)
                    or not isinstance(call["function"].get("arguments"), str)):
                raise ContractError("input_requires_openai_function_calls")
            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id or call_id in seen:
                raise ContractError("missing_or_reused_input_call_id")
            try:
                if call_errors(parse_call(call), schemas):
                    raise ValueError("input_call_schema")
            except (ValueError, TypeError) as exc:
                raise ContractError("invalid_input_tool_call") from exc
            seen.add(call_id)
            pending.add(call_id)
    if pending:
        raise ContractError("missing_input_tool_results")


def _references(case):
    expected = case["expected"]
    calls = [parse_call(call) for call in expected["calls"]]
    if "kind" in case and case["kind"] != expected["kind"]:
        raise ContractError("case_kind_expected_mismatch")
    for key in ("ground_truth", "reference"):
        if key not in case:
            continue
        value = case[key]
        if key == "reference":
            if not isinstance(value, dict):
                raise ContractError("reference_must_be_object")
            if "content" in value and value["content"] is not None and not isinstance(value["content"], str):
                raise ContractError("reference_content_must_be_text")
            if "role" in value and value["role"] != "assistant":
                raise ContractError("reference_must_be_assistant")
            if expected["kind"] == "text" and (
                    not isinstance(value.get("content"), str) or not value["content"].strip()):
                raise ContractError("text_reference_requires_content")
            value = value.get("tool_calls", [])
        try:
            if not isinstance(value, list) or canonical([parse_call(call) for call in value]) != canonical(calls):
                raise ValueError("reference_mismatch")
        except (ValueError, TypeError) as exc:
            raise ContractError("reference_expected_mismatch") from exc


def _validate(bundle, config, mode, model):
    evaluate(bundle, mode)  # Check every expectation, not just the first case.
    if bundle.get("schema") != "retail-evaluation-input-v1":
        raise ContractError("unsupported_evaluation_input_schema")
    if bundle.get("records") or "inference" in bundle:
        raise ContractError("run_requires_unexecuted_input_and_separate_config")
    if model not in bundle.get("models", list(DEFAULT_MODELS)):
        raise ContractError("model_not_declared_in_input")
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
        raise ContractError("evaluation_config_requires_exact_fields")
    targets = config["targets"]
    if (not isinstance(targets, dict) or model not in targets
            or any(not isinstance(key, str) or not key.strip()
                   or not isinstance(value, str) or not value.strip()
                   for key, value in targets.items())):
        raise ContractError("explicit_model_targets_required")
    for key in ("max_completion_tokens", "max_model_calls", "max_tool_calls"):
        if type(config[key]) is not int or config[key] <= 0:
            raise ContractError(f"positive_integer_{key}_required")
    url = config["base_url"]
    if not isinstance(url, str):
        raise ContractError("https_openai_v1_endpoint_required")
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or any(char.isspace() for char in url)):
        raise ContractError("https_openai_v1_endpoint_required")
    # Construction is lazy and also validates token/timeout/endpoint settings.
    OpenAITransport(base_url=url, max_completion_tokens=config["max_completion_tokens"],
                    timeout_seconds=config["timeout_seconds"])
    for case in bundle["cases"]:
        tools = case.get("tools", bundle.get("tools"))
        if mode == "next-action":
            _messages(case.get("messages"), tools)
            _references(case)
        else:
            from ..retail import RetailSession
            session = RetailSession()
            if not isinstance(case.get("user_input"), str) or not case["user_input"].strip():
                raise ContractError("live_e2e_requires_user_input")
            if (case.get("system_prompt") != session.system_prompt
                    or canonical(tools) != canonical(session.tools)):
                raise ContractError("retail_contract_differs_from_input")


def _response(record):
    if not record:
        return None
    return record.get("message") if "message" in record else {
        "answer": record.get("answer"), "events": record.get("events")}


def _review_rows(bundle, mode):
    scores = evaluate(bundle, mode)
    cases = {case["case_id"]: case for case in bundle["cases"]}
    records = {(record["case_id"], record["model"]): record for record in bundle["records"]}
    rows = []
    for score in scores["rows"]:
        case = cases[score["case_id"]]
        record = records.get((score["case_id"], score["model"]))
        rows.append({
            "case_id": score["case_id"], "model": score["model"],
            "category": canonical(case.get("category")),
            "evidence_sha256": evidence_sha256(record) if record is not None else "",
            "messages": canonical(case.get("messages", [
                {"role": "system", "content": case.get("system_prompt")},
                {"role": "user", "content": case.get("user_input")}])),
            "expected": canonical(case["expected"]), "reference": canonical(case.get("reference")),
            "response": canonical(_response(record)),
            "status": score["status"],
            "reasons": canonical(score["technical_failures"] + score["quality_failures"]),
            "latency_seconds": canonical(score["latency_seconds"]), "usage": canonical(score["usage"]),
            "decision": "", "reviewer": "", "notes": "",
        })
    return rows


def _csv(path, fields, rows):
    with Path(path).open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _save(directory, bundle, execution):
    scores = evaluate(bundle, execution["mode"])
    if execution.get("grading"):
        from .grading import grading_overhead
        scores["grading"] = grading_overhead([{"model": execution["model"], **execution["grading"]}])
    write_json(directory / "evidence.json", bundle)
    write_json(directory / "scores.json", scores)
    execution.update(input_sha256=sha256(directory / "input.json"),
                     evidence_sha256=sha256(directory / "evidence.json"))
    write_json(directory / "execution.json", execution)
    return scores


def run_evaluation(input_path, config_path, mode, model, run_dir, *, invoke=None):
    """Validate everything, freeze input, then run one selected model without retry."""
    raw = Path(input_path).read_bytes()
    bundle = loads(raw.decode("utf-8-sig"))
    config = read_json(config_path)
    _validate(bundle, config, mode, model)
    directory = Path(run_dir)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "input.json", bundle)
    started = _now()
    execution = {
        "schema": "retail-evaluation-execution-v1", "mode": mode, "model": model,
        "target": config["targets"][model], "config": deepcopy(config), "started_at": started,
        "source_input_sha256": hashlib.sha256(raw).hexdigest(),
    }
    write_json(directory / "execution-start.json", execution)
    transport = invoke if invoke is not None else OpenAITransport(
        base_url=config["base_url"], max_completion_tokens=config["max_completion_tokens"],
        timeout_seconds=config["timeout_seconds"])
    recorded = RecordedModel(transport, journal=Journal(directory), target=execution["target"])
    evidence = deepcopy(bundle)
    evidence.update(models=[model], records=[], evidence_kind=LIVE_EVIDENCE_KIND)
    for case in bundle["cases"]:
        tools = case.get("tools", bundle.get("tools"))
        if mode == "next-action":
            record = run_next_action(case, model, recorded, tools)
        else:
            from ..retail import RetailSession
            record = run_case(case, model, recorded, RetailSession, tools=tools,
                              max_model_calls=config["max_model_calls"],
                              max_tool_calls=config["max_tool_calls"])
        record["provenance"] = {
            "runtime": {
                "kind": "local_direct_model", "model": execution["target"],
                "endpoint": config["base_url"],
                "model_identity_basis": "configured_target_with_response_identity_when_available",
                "latency_scope": "local_wall_clock_model_and_tool_loop",
            },
            "input_sha256": execution["source_input_sha256"],
            "evidence_kind": LIVE_EVIDENCE_KIND,
            "identity_assurance": "configured_target_and_local_runner_not_remote_attestation",
        }
        evidence["records"].append(record)
        write_json(directory / f"record-{len(evidence['records']):06d}.json", record)
        scorer = score_next_action if mode == "next-action" else score_e2e
        score = scorer(case, record, tools, model)
        if score["technical_failures"]:
            break
    scores = evaluate(evidence, mode)
    execution.update(finished_at=_now(), complete=(
        scores["observed_records"] == scores["scheduled_slots"]
        and not scores["overall"]["status_counts"]["technical_failure"]))
    return _save(directory, evidence, execution), execution["complete"]


def _load_run(directory):
    directory = Path(directory)
    execution = read_json(directory / "execution.json")
    if execution.get("schema") != "retail-evaluation-execution-v1":
        raise ContractError("unsupported_execution_schema")
    for name in ("input", "evidence"):
        if sha256(directory / f"{name}.json") != execution.get(f"{name}_sha256"):
            raise ContractError(f"changed_{name}_snapshot")
    bundle, original = read_json(directory / "evidence.json"), read_json(directory / "input.json")
    _validate(original, execution["config"], execution["mode"], execution["model"])
    expected = deepcopy(original)
    expected.update(models=[execution["model"]], records=bundle.get("records"),
                    evidence_kind=LIVE_EVIDENCE_KIND)
    if canonical(expected) != canonical(bundle) or execution["target"] != execution["config"]["targets"][execution["model"]]:
        raise ContractError("evidence_input_or_target_mismatch")
    report = evaluate(bundle, execution["mode"])
    grading = execution.get("grading")
    if grading is not None:
        from .grading import OVERHEAD_FIELDS, _digest, grading_input, protocol
        if (grading.get("protocol") != protocol(grading["protocol"]["config"])
                or grading.get("protocol_sha256") != _digest(grading["protocol"])
                or sha256(directory / "grading.json") != grading.get("artifact_sha256")):
            raise ContractError("changed_grading_protocol_or_artifact")
        metadata = read_json(directory / "grading.json")
        if (metadata.get("protocol") != grading["protocol"]
                or metadata.get("protocol_sha256") != grading["protocol_sha256"]
                or any(grading.get(key) != metadata.get(key) for key in OVERHEAD_FIELDS)):
            raise ContractError("grading_protocol_mismatch")
        assessments = {(item["case_id"], item["model"]): item for item in metadata["assessments"]}
        cases = {case["case_id"]: case for case in bundle["cases"]}
        rows = {(row["case_id"], row["model"]): row for row in report["rows"]}
        for record in bundle["records"]:
            key = (record["case_id"], record["model"])
            review = rows[key]["model_review"]
            case = cases[record["case_id"]]
            if (review is None or review["protocol"] != grading["protocol"]
                    or assessments.get(key) != {"case_id": key[0], "model": key[1], **review}
                    or review.get("grading_input_sha256") != _digest(grading_input(
                        case, record, case.get("tools", bundle.get("tools")), execution["mode"]))):
                raise ContractError("invalid_or_stale_model_review")
    elif any("model_review" in record for record in bundle["records"]):
        raise ContractError("model_review_requires_grading_provenance")
    return original, bundle, execution


def review_sheet(run_dir):
    """Explicit legacy opt-in; standard runs never require review CSV input."""
    _, evidence, execution = _load_run(run_dir)
    if execution.get("grading"):
        raise ContractError("human_review_sheet_requires_ungraded_run")
    _csv(Path(run_dir) / "reviews.csv", REVIEW_FIELDS, _review_rows(evidence, execution["mode"]))


def review_run(run_dir, output_dir):
    """Only decision/reviewer/notes are editable; bind reviews to exact responses."""
    original, evidence, execution = _load_run(run_dir)
    if execution.get("grading"):
        raise ContractError("human_review_requires_ungraded_run")
    expected = _review_rows(evidence, execution["mode"])
    with (Path(run_dir) / "reviews.csv").open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(REVIEW_FIELDS):
            raise ContractError("review_csv_columns_changed")
        submitted = list(reader)
    if len(submitted) != len(expected):
        raise ContractError("review_case_count_mismatch")
    indexed = {(row["case_id"], row["model"]): row for row in expected}
    seen = set()
    records = {(record["case_id"], record["model"]): record for record in evidence["records"]}
    for row in submitted:
        key = (row.get("case_id"), row.get("model"))
        if (key not in indexed or key in seen or set(row) != set(REVIEW_FIELDS)
                or any(not isinstance(value, str) for value in row.values())):
            raise ContractError("review_case_identity_mismatch")
        seen.add(key)
        if any(row[field] != indexed[key][field] for field in REVIEW_FIELDS if field not in USER_REVIEW_FIELDS):
            raise ContractError("stale_or_changed_review_evidence")
        decision, reviewer, notes = (row[field].strip() for field in ("decision", "reviewer", "notes"))
        if not decision:
            if reviewer or notes:
                raise ContractError("review_decision_required")
            continue
        if decision not in {"success", "failure", "needs_review"}:
            raise ContractError("review_decision_must_be_success_failure_or_needs_review")
        record = records.get(key)
        if record is None:
            raise ContractError("cannot_review_missing_record")
        if decision == "needs_review":
            record.pop("review", None)
            continue
        if not reviewer or not notes:
            raise ContractError("reviewer_and_notes_required")
        record["review"] = {
            "decision": {"success": "confirmed_success", "failure": "quality_failure"}[decision],
            "reviewer": reviewer, "notes": notes, "reviewed_at": _now(),
            "evidence_sha256": evidence_sha256(record),
        }
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "input.json", original)
    _csv(directory / "review-input.csv", REVIEW_FIELDS, submitted)
    execution.update(reviewed_at=_now(), review_source_evidence_sha256=execution["evidence_sha256"])
    return _save(directory, evidence, execution)


def _decision(before, after):
    ranks = {"quality_failure": 0, "confirmed_success": 1}
    if before not in ranks or after not in ranks:
        return "incomparable"
    return ("improvement" if ranks[after] > ranks[before] else
            "regression" if ranks[after] < ranks[before] else "no_change")


def _common_contract(original, execution):
    frozen = {key: value for key, value in original.items() if key != "models"}
    settings = {key: value for key, value in execution["config"].items() if key != "targets"}
    return {"input": frozen, "mode": execution["mode"], "settings": settings,
            "grading_protocol": deepcopy(execution.get("grading", {}).get("protocol"))}


def _require_common_contract(runs):
    reference = canonical(_common_contract(runs[0][0], runs[0][2]))
    if any(canonical(_common_contract(original, execution)) != reference
           for original, _, execution in runs[1:]):
        raise ContractError("comparison_cases_tools_source_schema_or_inference_mismatch")
    if runs[0][2].get("grading"):
        identities, unknown = set(), 0
        for _, bundle, _ in runs:
            for record in bundle["records"]:
                review = record.get("model_review", {})
                if review.get("state") == "skipped":
                    continue
                response = review.get("response")
                identity = response.get("response_model") if isinstance(response, dict) else None
                if isinstance(identity, str) and identity.strip():
                    identities.add(identity)
                else:
                    unknown += 1
        if len(identities) > 1:
            raise ContractError("comparison_grader_response_model_mismatch")
        return {"response_models": sorted(identities), "response_model_unknown_calls": unknown}
    return None


def combine_runs(run_dirs, output_dir):
    """Combine same-cohort model evidence offline without touching bound records."""
    if not isinstance(run_dirs, (list, tuple)) or len(run_dirs) < 2:
        raise ContractError("combine_requires_at_least_two_model_runs")
    runs = [_load_run(directory) for directory in run_dirs]
    _require_common_contract(runs)
    models = [execution["model"] for _, _, execution in runs]
    if len(set(models)) != len(models):
        raise ContractError("combine_requires_unique_model_labels")
    original = deepcopy(runs[0][0])
    original["models"] = models
    evidence = deepcopy(original)
    evidence.update(evidence_kind=LIVE_EVIDENCE_KIND,
                    records=[deepcopy(record) for _, bundle, _ in runs for record in bundle["records"]])
    mode = runs[0][2]["mode"]
    scores = evaluate(evidence, mode)
    execution = {
        "schema": "retail-evaluation-combined-execution-v1", "mode": mode, "models": models,
        "combined_at": _now(),
        "config": {
            **deepcopy(_common_contract(original, runs[0][2])["settings"]),
            "targets": {item["model"]: item["target"] for _, _, item in runs},
        },
        "sources": [
            {key: deepcopy(item[key]) for key in
             ("model", "target", "input_sha256", "evidence_sha256", "source_input_sha256", "grading")
             if key in item}
            for _, _, item in runs],
        "complete": (scores["observed_records"] == scores["scheduled_slots"]
                     and not scores["overall"]["status_counts"]["technical_failure"]),
    }
    if runs[0][2].get("grading"):
        from .grading import grading_overhead
        scores["grading"] = grading_overhead([
            {"model": item["model"], **item["grading"]} for _, _, item in runs])
        execution["grading"] = {
            "protocol": deepcopy(runs[0][2]["grading"]["protocol"]),
            "source_artifact_sha256": [item["grading"]["artifact_sha256"] for _, _, item in runs],
            "complete": all(item["grading"]["complete"] for _, _, item in runs),
        }
        execution["complete"] = execution["complete"] and execution["grading"]["complete"]
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "input.json", original)
    write_json(directory / "evidence.json", evidence)
    write_json(directory / "scores.json", scores)
    execution.update(input_sha256=sha256(directory / "input.json"),
                     evidence_sha256=sha256(directory / "evidence.json"))
    write_json(directory / "execution.json", execution)
    return scores


def compare_runs(before_dir, after_dir, output_dir):
    """Compare identical tasks/settings, permitting only model/target differences."""
    before_input, before, before_execution = _load_run(before_dir)
    after_input, after, after_execution = _load_run(after_dir)
    grader_identity = _require_common_contract([(before_input, before, before_execution),
                                                (after_input, after, after_execution)])
    reports = [evaluate(bundle, execution["mode"])
               for bundle, execution in ((before, before_execution), (after, after_execution))]
    records = [{record["case_id"]: record for record in bundle["records"]} for bundle in (before, after)]
    model_judged = bool(before_execution.get("grading"))
    rows = []
    for case, left, right in zip(before_input["cases"], reports[0]["rows"], reports[1]["rows"]):
        row = {"case_id": case["case_id"], "category": deepcopy(case.get("category")),
            "messages": deepcopy(case.get("messages", [
            {"role": "system", "content": case.get("system_prompt")},
            {"role": "user", "content": case.get("user_input")}])),
            "expected": deepcopy(case["expected"]), "reference": deepcopy(case.get("reference")),
            "decision": _decision(
                {"success": "confirmed_success", "failure": "quality_failure"}.get(left["automatic_decision"]),
                {"success": "confirmed_success", "failure": "quality_failure"}.get(right["automatic_decision"]))
                if model_judged else _decision(left["status"], right["status"])}
        for side, score, saved, execution in (
                ("before", left, records[0], before_execution),
                ("after", right, records[1], after_execution)):
            record = saved.get(case["case_id"])
            row[side] = {
                "model": execution["model"], "target": execution["target"],
                "status": score["status"], "reasons": score["technical_failures"] + score["quality_failures"],
                "response": deepcopy(_response(record)), "review": score["review"],
                "latency_seconds": score["latency_seconds"], "usage": score["usage"],
                "provenance": score["provenance"],
                "response_model": record.get("response_model") if record else None,
                "response_id": record.get("response_id") if record else None,
                "automatic_decision": score["automatic_decision"],
                "assessment_source": score["assessment_source"],
                "model_review": score["model_review"],
                "judge_reason": score["model_review"]["reason"] if score["model_review"] else None,
            }
        rows.append(row)
    counts = {value: sum(row["decision"] == value for row in rows)
              for value in ("improvement", "regression", "no_change", "incomparable")}
    decision = ("incomparable" if counts["incomparable"] else
                "improvement" if counts["improvement"] > counts["regression"] else
                "regression" if counts["regression"] > counts["improvement"] else "no_change")
    comparison = {
        "schema": "retail-evaluation-comparison-v1", "mode": before_execution["mode"],
        "decision": decision, "decision_counts": counts, "cases": rows,
        "decision_basis": ("provisional_model_judgment_with_deterministic_checks; unknown_or_needs_review_is_incomparable"
                           if model_judged else
                           "confirmed_success_vs_quality_failure_only; pending_or_technical_is_incomparable"),
        "assessment_method": "model" if model_judged else "legacy_human_review",
        "grading_protocol": deepcopy(before_execution.get("grading", {}).get("protocol")),
        "grader_response_identity": grader_identity,
        "overall": {"before": reports[0]["overall"], "after": reports[1]["overall"]},
        "before_evidence_sha256": before_execution["evidence_sha256"],
        "after_evidence_sha256": after_execution["evidence_sha256"],
    }
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "comparison.json", comparison)
    flat_rows = []
    for row in rows:
        flat = {key: canonical(row[key]) if key in ("category", "messages", "expected", "reference") else row[key]
                for key in ("case_id", "category", "decision", "messages", "expected", "reference")}
        for side in ("before", "after"):
            flat.update({f"{side}_{key}": canonical(value) if not isinstance(value, str) else value
                         for key, value in row[side].items()})
        flat_rows.append(flat)
    _csv(directory / "comparison.csv", list(flat_rows[0]), flat_rows)
    def cell(value):
        text = value if isinstance(value, str) else canonical(value)
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;").replace("\r", "").replace("\n", "<br>")
    lines = ["# Evaluation comparison", "",
             f"Assessment method: {comparison['assessment_method']}. Decision: {decision}.",
             "Model judgments are provisional; they are not human-reviewed business success.",
             "Full task context, responses, usage, and provenance are in comparison.json.", "",
             "| Case | Before | After | Change | Before reason | After reason | Before source | After source | Before response | After response | Reference / expected |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        values = [row["case_id"]]
        values.extend(row[side]["automatic_decision"] if model_judged else row[side]["status"]
                      for side in ("before", "after"))
        values.append(row["decision"])
        values.extend(row[side]["judge_reason"] or row[side]["reasons"] for side in ("before", "after"))
        values.extend(row[side]["assessment_source"] for side in ("before", "after"))
        values.extend(row[side]["response"] for side in ("before", "after"))
        values.append({"reference": row["reference"], "expected": row["expected"]})
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    with (directory / "comparison.md").open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    return comparison
