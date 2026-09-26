"""Run a model or grader; compare, combine, or score saved evidence offline."""

import argparse
import hashlib
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from foundry_distillation_lab.evaluation import (  # noqa: E402
    evaluate, import_hosted, prepare_next_actions,
)
from foundry_distillation_lab.evaluation.schema import ContractError, loads  # noqa: E402
from foundry_distillation_lab.evaluation.workflow import (  # noqa: E402
    combine_runs, compare_runs, review_run, review_sheet, run_evaluation,
)
from foundry_distillation_lab.evaluation.grading import grade_run  # noqa: E402
from foundry_distillation_lab.evaluation.study import study  # noqa: E402
from foundry_distillation_lab.io import read_jsonl, sha256, write_json  # noqa: E402


def _mode_input(parser):
    parser.add_argument("--mode", required=True, choices=("next-action", "e2e"))
    parser.add_argument("--input", required=True, type=Path, help="UTF-8 JSON evaluation input")


def _offline_args(parser):
    _mode_input(parser)
    parser.add_argument("--output", required=True, type=Path, help="New JSON output; never overwritten")
    parser.add_argument("--prepare-next-actions", action="store_true",
                        help="Offline: convert prepared development JSONL into an input bundle")
    parser.add_argument("--import-hosted", type=Path,
                        help="Offline: import Hosted execution evidence into an E2E bundle")
    parser.add_argument("--model-label", help="Declared model label for --import-hosted only")


def _parser(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    # Retain the established flag-only offline interface.
    if argv and argv[0].startswith("--") and argv[0] not in ("--help",):
        _offline_args(parser)
        parser.set_defaults(command="score")
        return parser
    commands = parser.add_subparsers(dest="command", required=True)
    experiment = commands.add_parser("study", help="Run one new LOCAL teacher/base/fine_tuned experiment")
    experiment.add_argument("--input", required=True, type=Path)
    experiment.add_argument("--config", required=True, type=Path)
    experiment.add_argument("--grading-config", required=True, type=Path)
    experiment.add_argument("--output-dir", required=True, type=Path,
                            help="Output root; existing experiments get a new numbered sibling")
    run = commands.add_parser("run", help="Execute direct model requests; no retries")
    _mode_input(run)
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--model", required=True, help="A model label declared by input and config")
    run.add_argument("--run-dir", required=True, type=Path, help="New exclusive run directory")
    grade = commands.add_parser("grade", help="Automatically grade saved responses with a fixed blinded rubric")
    grade.add_argument("--run-dir", required=True, type=Path)
    grade.add_argument("--config", required=True, type=Path)
    grade.add_argument("--output-dir", required=True, type=Path)
    compare = commands.add_parser("compare", help="Compare two compatible saved runs offline")
    compare.add_argument("--before", required=True, type=Path)
    compare.add_argument("--after", required=True, type=Path)
    compare.add_argument("--output-dir", required=True, type=Path)
    combine = commands.add_parser("combine", help="Combine same-cohort selected-model runs offline")
    combine.add_argument("--run-dirs", required=True, nargs="+", type=Path)
    combine.add_argument("--output-dir", required=True, type=Path)
    review = commands.add_parser("review", help="Import decision/reviewer/notes from run reviews.csv offline")
    review.add_argument("--run-dir", required=True, type=Path)
    review.add_argument("--output-dir", required=True, type=Path)
    sheet = commands.add_parser("review-sheet", help="Explicit optional legacy human-review CSV export")
    sheet.add_argument("--run-dir", required=True, type=Path)
    _offline_args(commands.add_parser("score", help="Score or prepare/import evidence offline"))
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _parser(argv)
    args = parser.parse_args(argv)
    if args.command == "study":
        try:
            summary = study(args.input, args.config, args.grading_config, args.output_dir)
        except Exception as exc:
            print(f"evaluation stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
            for note in getattr(exc, "__notes__", []):
                print(note, file=sys.stderr)
            return 2
        print(f"study: {summary['status']}; output directory: {summary['output_dir']}")
        print(f"three-model table: {Path(summary['output_dir']) / 'comparison.csv'}")
        print(f"summary: {Path(summary['output_dir']) / 'summary.json'}")
        return 0 if summary["complete"] else 2
    try:
        if args.command == "run":
            report, complete = run_evaluation(args.input, args.config, args.mode, args.model, args.run_dir)
            print(f"{args.mode}: {report['observed_records']}/{report['scheduled_slots']} records; {args.run_dir}")
            return 0 if complete else 2
        if args.command == "compare":
            comparison = compare_runs(args.before, args.after, args.output_dir)
            print(f"comparison: {comparison['decision']}; {args.output_dir}")
            return 0
        if args.command == "grade":
            report, complete = grade_run(args.run_dir, args.config, args.output_dir)
            print(f"model graded: {report['overall']['automatic_decision_counts']}; {args.output_dir}")
            return 0 if complete else 2
        if args.command == "review-sheet":
            review_sheet(args.run_dir)
            print(f"optional legacy review sheet: {args.run_dir}")
            return 0
        if args.command == "combine":
            report = combine_runs(args.run_dirs, args.output_dir)
            print(f"combined {len(report['per_model'])} models; "
                  f"{report['observed_records']}/{report['scheduled_slots']} records; {args.output_dir}")
            return 0
        if args.command == "review":
            review_run(args.run_dir, args.output_dir)
            print(f"review imported offline: {args.output_dir}")
            return 0
        if args.prepare_next_actions:
            if args.mode != "next-action" or args.import_hosted or args.model_label:
                raise ContractError("preparation_requires_next-action_mode_without_import_flags")
            bundle = prepare_next_actions(read_jsonl(args.input), sha256(args.input))
            write_json(args.output, bundle)
            print(f"prepared {len(bundle['cases'])} cases; no predictions or scores: {args.output}")
            return 0
        bundle = loads(args.input.read_bytes().decode("utf-8-sig"))
        if args.import_hosted:
            if args.mode != "e2e" or not args.model_label:
                raise ContractError("hosted_import_requires_e2e_model_label")
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
            write_json(args.output, imported)
            print(f"imported {len(captures)} Hosted evidence records offline; {args.output}")
            return 0
        if args.model_label:
            raise ContractError("--model-label_requires_--import-hosted")
        report = evaluate(bundle, args.mode)
        write_json(args.output, report)
        print(f"{args.mode}: {report['observed_records']}/{report['scheduled_slots']} records; {args.output}")
        return 0
    except (OSError, ValueError, KeyError, ImportError, PermissionError) as exc:
        print(f"evaluation stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
