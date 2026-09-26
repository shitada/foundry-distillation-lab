"""Prepare/import offline; --invoke sends one recorded azd request."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from foundry_distillation_lab.training.collection import import_traces, prepare_collection
from foundry_distillation_lab.training import invocation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="Mode-specific export/prompt JSONL, collection plan, or invocation plan")
    parser.add_argument("--format", choices=("conversation", "responses-capture"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true", help="Prepare hosted collection plan offline")
    mode.add_argument("--prepare-invocation", action="store_true", help="Prepare one-shot azd invocation offline")
    mode.add_argument("--invoke", action="store_true", help="Send one network request using azd; never auto-retry")
    parser.add_argument("--config", type=Path, help="Required with either prepare mode")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.invoke:
        if args.config or args.format:
            parser.error("--invoke forbids --config and --format")
        result = invocation.invoke(args.input, run_dir=args.output_dir)
    elif args.prepare or args.prepare_invocation:
        if args.config is None or args.format:
            parser.error("Prepare modes require --config and forbid --format")
        operation = invocation.prepare if args.prepare_invocation else prepare_collection
        result = operation(args.input, args.config, args.output_dir)
    else:
        if not args.format or args.config:
            parser.error("Import requires --format and forbids --config")
        result = import_traces(args.input, args.output_dir, args.format)
    print(json.dumps(result,
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
