"""Prepare locally, or perform one explicitly approved training operation."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from foundry_distillation_lab.training import jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Offline JSONL normalization and hash-bound plan")
    prepare.add_argument("--train", type=Path, required=True)
    prepare.add_argument("--validation", type=Path, required=True)
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare_submit = commands.add_parser("prepare-submit", help="Offline payload using known upload receipts")
    prepare_submit.add_argument("--upload-plan", type=Path, required=True)
    prepare_submit.add_argument("--train-receipt", type=Path, required=True)
    prepare_submit.add_argument("--validation-receipt", type=Path, required=True)
    prepare_submit.add_argument("--output", type=Path, required=True)
    for name in ("upload", "submit", "status"):
        command = commands.add_parser(name, help="Guarded network operation; no automatic retries")
        command.add_argument("--execute", action="store_true")
        command.add_argument("--approval", type=Path)
        command.add_argument("--run-dir", type=Path, required=True)
        if name == "status":
            command.add_argument("--receipt", type=Path, required=True)
            command.add_argument("--observation-id", required=True)
        else:
            command.add_argument("--plan", type=Path, required=True)
        if name == "upload":
            command.add_argument("--role", choices=("train", "validation"), required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = jobs.prepare(args.train, args.validation, args.config, args.output_dir)
    elif args.command == "prepare-submit":
        result = jobs.prepare_submission(args.upload_plan, args.train_receipt,
                                         args.validation_receipt, args.output)
    else:
        options = dict(execute=args.execute, approval_path=args.approval, run_dir=args.run_dir)
        if args.command == "upload":
            result = jobs.upload(args.plan, args.role, **options)
        elif args.command == "submit":
            result = jobs.submit(args.plan, **options)
        else:
            result = jobs.status(args.receipt, observation_id=args.observation_id, **options)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
