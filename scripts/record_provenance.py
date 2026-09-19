"""Record hashes without publishing machine paths, cloud identifiers or raw runs."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from foundry_distillation_lab.io import sha256, write_json

UPSTREAM = "https://github.com/microsoft-foundry/fine-tuning"
BASE = "2de8f491026472358919b35e473ab42393a686a7"
FORK = "7a06f181eba475e05e608924de8e84d79a1c5978"
SOURCES = [
    ("LICENSE", "LICENSE", "unchanged"),
    ("Demos/TracesDistillation/fixtures/zava_system_prompt.md",
     "src/foundry_distillation_lab/retail/contract/zava_system_prompt.md", "unchanged Japanese adaptation"),
    ("Demos/TracesDistillation/fixtures/zava_tools.json",
     "src/foundry_distillation_lab/retail/contract/zava_tools.json", "unchanged Japanese adaptation"),
    ("Demos/TracesDistillation/agent/src/zava-traces-demo/synthetic_store.py",
     "src/foundry_distillation_lab/retail/synthetic_store.py", "unchanged Japanese synthetic business"),
    ("Demos/TracesDistillation/fixtures/transform_traces.py",
     "src/foundry_distillation_lab/datasets", "design reference; rewritten conservative normalizer"),
    ("Demos/TracesDistillation/fixtures/prepare_baseline.py",
     "src/foundry_distillation_lab/datasets", "adapted split and next-action design"),
    ("Demos/TracesDistillation/fixtures/train_student.py",
     "src/foundry_distillation_lab/training", "adapted training safety design"),
    ("Demos/TracesDistillation/fixtures/deploy_student.py",
     "src/foundry_distillation_lab/training", "adapted ownership and approval design"),
    ("Demos/TracesDistillation/e2e-agent/evaluation_runner.py",
     "src/foundry_distillation_lab/evaluation", "adapted evaluation evidence design"),
    ("Demos/TracesDistillation/e2e-agent/evaluation_scoring.py",
     "src/foundry_distillation_lab/evaluation", "adapted provisional scoring design"),
    ("Demos/TracesDistillation/report_step17_offline.py",
     "src/foundry_distillation_lab/reporting", "cost-analysis reference; no historical result copied"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    entries = []
    for source, target, adaptation in SOURCES:
        source_path = args.source_root.joinpath(*source.split("/"))
        target_path = repo.joinpath(*target.split("/"))
        entry = {"source_path": source, "local_source_sha256": sha256(source_path),
                 "target": target, "adaptation": adaptation}
        if adaptation.startswith("unchanged"):
            entry["target_sha256"] = sha256(target_path)
            if entry["target_sha256"] != entry["local_source_sha256"]:
                raise ValueError(f"Expected exact source copy: {target}")
        entries.append(entry)
    write_json(args.output, {
        "upstream_repository": UPSTREAM, "upstream_reference_commit": BASE,
        "japanese_fork_repository": "https://github.com/shitada/fine-tuning",
        "local_source_head": FORK,
        "local_worktree_included_uncommitted_files": True,
        "note": "Local file hashes, not HEAD alone, identify the adapted worktree evidence. "
                "An uncommitted file is not asserted to exist in upstream or fork commits.",
        "license": "MIT; upstream LICENSE verified from the pinned local git object",
        "sources": entries,
    })


if __name__ == "__main__":
    main()
