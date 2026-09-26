"""Start supervised training once, or observe an existing run without mutations."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from foundry_distillation_lab.training import jobs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="Validate, snapshot, upload, wait for files, submit once")
    start.add_argument("--train", type=Path, required=True)
    start.add_argument("--validation", type=Path, required=True)
    start.add_argument("--config", type=Path, required=True)
    status = commands.add_parser("status", help="GET-only job/file observations with automatic IDs")
    status.add_argument("--wait", action="store_true", help="Wait for a terminal state within the deadline")
    for command in (start, status):
        command.add_argument("--run-dir", type=Path, required=True)
        command.add_argument("--timeout-seconds", type=float, default=3600)
        command.add_argument("--poll-seconds", type=float, default=10)
    args = parser.parse_args(argv)
    options = dict(timeout_seconds=args.timeout_seconds, poll_seconds=args.poll_seconds)
    try:
        if args.command == "start":
            result = jobs.start(args.train, args.validation, args.config, args.run_dir, **options)
            exit_code = 1 if result["response"].get("status") in {"failed", "cancelled"} else 0
        else:
            result = jobs.status(args.run_dir, wait=args.wait, **options)
            exit_code = 0 if result["outcome"] in {"succeeded", "pending"} else 1
    except Exception as exc:
        print(json.dumps({"outcome": "timeout" if isinstance(exc, TimeoutError) else "error",
                          "error": str(exc),
                          "next_step": "Inspect run evidence and use status; never blindly resend."}),
              file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
