"""One fresh, bounded LOCAL three-model experiment; no deployment or retries."""

from copy import deepcopy
import hashlib
import os
from pathlib import Path

from ..io import write_json
from .grading import grade_run, grading_overhead, protocol
from .schema import canonical, loads
from .scoring import evaluate
from .workflow import (
    _csv, _load_run, _now, _require_common_contract, _response, _validate,
    compare_runs, run_evaluation,
)


MODELS = ("teacher", "base", "fine_tuned")
PAIRS = (
    ("base", "fine_tuned", "base-vs-fine-tuned"),
    ("teacher", "fine_tuned", "teacher-vs-fine-tuned"),
    ("teacher", "base", "teacher-vs-base"),
)


def _allocate_directory(output_dir):
    """Exclusive mkdir arbitrates concurrent invocations without reusing evidence."""
    root = Path(output_dir).absolute()
    for index in range(1, 10001):
        candidate = root if index == 1 else root.with_name(f"{root.name}-{index:03d}")
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return candidate.resolve()
    raise FileExistsError(f"study_output_allocation_exhausted: {root}")


def _case_table(runs):
    _require_common_contract(runs)
    indexed = {}
    for _, evidence, execution in runs:
        report = evaluate(evidence, "e2e")
        records = {record["case_id"]: record for record in evidence["records"]}
        indexed[execution["model"]] = (
            {row["case_id"]: row for row in report["rows"]}, records)
    rows = []
    for case in runs[0][0]["cases"]:
        row = {
            "case_id": case["case_id"], "category": canonical(case.get("category")),
            "user_input": case["user_input"], "expected": canonical(case["expected"]),
            "reference": canonical(case.get("reference")),
        }
        for model in MODELS:
            scores, records = indexed[model]
            score = scores[case["case_id"]]
            review = score["model_review"]
            reasons = score["technical_failures"] + score["quality_failures"]
            record = records.get(case["case_id"])
            values = {
                "verdict": score["automatic_decision"],
                "reason": review["reason"] if review else canonical(reasons),
                "status": score["status"], "assessment_source": score["assessment_source"],
                "response": canonical(_response(record)),
                "latency_seconds": canonical(score["latency_seconds"]),
                "usage": canonical(score["usage"]),
                "input_tokens": canonical(score["usage"]["input_tokens"]),
                "output_tokens": canonical(score["usage"]["output_tokens"]),
                "cached_input_tokens": canonical(score["usage"]["cached_input_tokens"]),
                "evidence_sha256": score["evidence_sha256"],
            }
            row.update({f"{model}_{key}": value for key, value in values.items()})
        rows.append(row)
    return rows


def study(input_path, config_path, grading_config_path, output_dir, *, invoke=None, judge=None):
    """Run and grade teacher/base/fine_tuned, then compare their bound evidence.

    A new invocation is a new experiment, including after an uncertain request.
    Technical failures retain missing slots and continue only to separate models.
    Unexpected orchestration failures persist a failed summary and propagate.
    """
    snapshots = {
        name: Path(path).read_bytes()
        for name, path in (("input.json", input_path), ("evaluation-config.json", config_path),
                           ("grading-config.json", grading_config_path))
    }
    parsed = {name: loads(raw.decode("utf-8-sig")) for name, raw in snapshots.items()}
    for model in MODELS:
        _validate(parsed["input.json"], parsed["evaluation-config.json"], "e2e", model)
    settings = protocol(parsed["grading-config.json"])
    directory = _allocate_directory(output_dir)
    summary = {
        "schema": "retail-evaluation-study-v1", "mode": "e2e",
        "runtime": "local_tool_loop", "output_dir": str(directory),
        "complete": False, "status": "running", "started_at": _now(),
        "models": list(MODELS), "per_model": {}, "comparisons": {}, "steps": [], "errors": [],
        "case_count": len(parsed["input.json"]["cases"]),
        "scheduled_slots": len(parsed["input.json"]["cases"]) * len(MODELS),
        "assessment_method": "provisional_model_judgment_with_deterministic_checks",
        "business_success_requires_human_review": True,
        "grading_protocol": settings,
        "snapshots": {name: hashlib.sha256(raw).hexdigest() for name, raw in snapshots.items()},
        "actual_cost": None,
    }

    def checkpoint(step, state, **details):
        event = {"step": step, "state": state, "at": _now(), **details}
        write_json(directory / f"step-{len(summary['steps']) + 1:03d}.json", event)
        summary["steps"].append(event)

    try:
        write_json(directory / "study-start.json", summary)
        for name, raw in snapshots.items():
            with (directory / name).open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        for model in MODELS:
            checkpoint(f"{model}:evaluate", "started")
            report, complete = run_evaluation(
                directory / "input.json", directory / "evaluation-config.json", "e2e",
                model, directory / model, invoke=invoke)
            summary["per_model"][model] = {
                "evaluation_complete": complete, "grading_complete": False,
                "scheduled_slots": report["scheduled_slots"],
                "observed_records": report["observed_records"], **deepcopy(report["overall"]),
            }
            checkpoint(f"{model}:evaluate", "complete" if complete else "incomplete",
                       summary=summary["per_model"][model])
            if not complete:
                summary["errors"].append({"step": f"{model}:evaluate", "error": "incomplete_evaluation",
                                          "status_counts": report["overall"]["status_counts"]})
            checkpoint(f"{model}:grade", "started")
            _, grade_complete = grade_run(
                directory / model, directory / "grading-config.json",
                directory / f"{model}-graded", invoke=judge)
            _, evidence, execution = _load_run(directory / f"{model}-graded")
            scored = evaluate(evidence, "e2e")
            summary["per_model"][model].update(
                deepcopy(scored["overall"]), grading_complete=grade_complete,
                complete=complete and grade_complete, grading=deepcopy(execution["grading"]),
                grading_artifact_sha256=execution["grading"]["artifact_sha256"],
                evidence_sha256=execution["evidence_sha256"],
                input_sha256=execution["input_sha256"], target=execution["target"])
            checkpoint(f"{model}:grade", "complete" if grade_complete else "incomplete",
                       summary=summary["per_model"][model])
            if not grade_complete:
                summary["errors"].append({"step": f"{model}:grade", "error": "incomplete_grading"})
        for before, after, name in PAIRS:
            checkpoint(name, "started")
            comparison = compare_runs(directory / f"{before}-graded",
                                      directory / f"{after}-graded", directory / name)
            summary["comparisons"][name] = {
                key: deepcopy(comparison[key]) for key in
                ("decision", "decision_counts", "before_evidence_sha256", "after_evidence_sha256")}
            checkpoint(name, "complete", comparison=summary["comparisons"][name])
        runs = [_load_run(directory / f"{model}-graded") for model in MODELS]
        rows = _case_table(runs)
        _csv(directory / "comparison.csv", list(rows[0]), rows)
        summary["grading"] = grading_overhead([
            {"model": model, **summary["per_model"][model]["grading"]} for model in MODELS])
        summary["observed_records"] = sum(item["observed_records"] for item in summary["per_model"].values())
        summary["complete"] = all(item["complete"] for item in summary["per_model"].values())
        summary.update(status="complete" if summary["complete"] else "incomplete", finished_at=_now())
    except Exception as exc:
        summary.update(status="failed", complete=False, finished_at=_now())
        summary["errors"].append({"error_type": type(exc).__name__, "error": str(exc)})
        write_json(directory / "summary.json", summary)
        exc.add_note(f"study output directory: {directory}")
        raise
    write_json(directory / "summary.json", summary)
    return summary
