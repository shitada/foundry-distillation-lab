"""Generate an offline ARM plan; deployment and cleanup require explicit approval."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from foundry_distillation_lab.io import read_json, write_json
from foundry_distillation_lab.training import deployment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Offline plan; no resource lookup")
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    for name in ("deploy", "status", "cleanup"):
        command = commands.add_parser(name)
        command.add_argument("--execute", action="store_true")
        command.add_argument("--approval", type=Path)
        command.add_argument("--run-dir", type=Path, required=True)
        if name == "cleanup":
            command.add_argument("--ownership", type=Path, required=True)
        else:
            command.add_argument("--plan", type=Path, required=True)
        if name == "status":
            command.add_argument("--observation-id", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = deployment.build_plan(read_json(args.config))
        write_json(args.output, result)
    else:
        options = dict(execute=args.execute, approval_path=args.approval, run_dir=args.run_dir)
        if args.command == "cleanup":
            result = deployment.cleanup(args.ownership, **options)
        elif args.command == "status":
            result = deployment.status(args.plan, observation_id=args.observation_id, **options)
        else:
            result = deployment.deploy(args.plan, **options)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
