"""Estimate public-retail costs, or render saved cost inputs offline."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from foundry_distillation_lab.reporting import write_report
from foundry_distillation_lab.reporting.bridge import prepare_from_files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path,
                        help="New report directory, or new cost-input JSON with --prepare-evaluation")
    parser.add_argument("--prepare-evaluation", action="store_true",
                        help="Prepare cost input from a saved evaluation report, without network access")
    parser.add_argument("--config", type=Path,
                        help="Optional extra USD costs / daily hosting hours for --estimate; "
                             "required prices and assumptions for --prepare-evaluation")
    parser.add_argument("--estimate", action="store_true",
                        help="Discover experiment records and retrieve public prices; no billing queries")
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"),
                        help="Experiment directory for --estimate (default: runs)")
    args = parser.parse_args(argv)
    if args.estimate:
        if args.input or args.prepare_evaluation:
            parser.error("--estimate cannot use --input/--prepare-evaluation")
        from foundry_distillation_lab.reporting.estimate import estimate
        try:
            result = estimate(args.config, args.runs_dir, args.output or args.runs_dir / "cost-report")
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, OverflowError) as exc:
            parser.exit(2, f"Cost estimation stopped: {exc}\n")
        print(f"Public-retail estimate: {result['output_dir']}; "
              f"{'cost inputs complete' if result['complete'] else 'missing inputs: see estimate.md'}; not invoiced cost")
        print(f"Final comparison graphs: {Path(result['output_dir']) / 'graphs.md'}")
        return 0 if result["complete"] else 2
    if args.input is None or args.output is None:
        parser.error("--input and --output are required for offline reporting")
    if bool(args.config) != args.prepare_evaluation:
        parser.error("--prepare-evaluation and --config must be supplied together")
    try:
        if args.prepare_evaluation:
            prepare_from_files(args.input, args.config, args.output)
        else:
            data = json.loads(args.input.read_text(encoding="utf-8-sig"))
            write_report(data, args.output)
    except (ValueError, TypeError, OSError, OverflowError) as exc:
        parser.exit(2, f"report: {exc}\n")
    if args.prepare_evaluation:
        print(f"Prepared evaluation-cohort cost input: {args.output}; not production evidence")
    else:
        print(f"Offline report written to {args.output}; decision draft: hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
