"""Prepare independent E2E development cases offline; reuse identical inputs."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from foundry_distillation_lab.evaluation.cases import prepare_cases


def main():
    parser = argparse.ArgumentParser(
        description="Prepare 11 policy-authored supplemental development E2E cases (not a final holdout)")
    parser.add_argument("--data-dir", type=Path, required=True,
                        help="Complete successful prepare_data.py output, including manifest and audit")
    parser.add_argument("--output", type=Path, required=True,
                        help="Evaluation input JSON; identical bundles reused, differing files never overwritten")
    args = parser.parse_args()
    try:
        bundle = prepare_cases(args.data_dir, args.output)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(2, f"Evaluation preparation stopped: {error}\n")
    print(f"Prepared {len(bundle['cases'])} supplemental development cases: {args.output}")


if __name__ == "__main__":
    main()
