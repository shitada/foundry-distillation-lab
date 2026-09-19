"""Offline only; no cloud credentials are read."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from foundry_distillation_lab.datasets.prepare import prepare


def main():
    parser = argparse.ArgumentParser(description="Validate Japanese traces and create four grouped splits")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    try:
        result = prepare(args.input, args.output, args.seed)
    except (ValueError, FileExistsError) as error:
        parser.exit(2, f"Preparation stopped: {error}\n")
    print({name: len(rows) for name, rows in result["partitions"].items()})


if __name__ == "__main__":
    main()
