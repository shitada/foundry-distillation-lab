"""Render offline aggregated costs without looking up prices or calling Azure."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from foundry_distillation_lab.reporting import write_report
from foundry_distillation_lab.reporting.bridge import prepare_from_files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path,
                        help="New report directory, or new cost-input JSON with --prepare-evaluation")
    parser.add_argument("--prepare-evaluation", action="store_true",
                        help="Prepare cost input from a saved evaluation report, without network access")
    parser.add_argument("--config", type=Path,
                        help="Explicit prices, fees and projection assumptions; required for preparation")
    args = parser.parse_args()
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


if __name__ == "__main__":
    main()
