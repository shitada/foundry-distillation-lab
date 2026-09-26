"""Create an allowlisted build context offline, never from the repository root."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil

RETAIL_FILES = ("__init__.py", "synthetic_store.py", "contract/zava_tools.json",
                "contract/zava_system_prompt.md")
TEMPLATE_FILES = ("main.py", "capture.py", "tools.py", "requirements.txt", "Dockerfile", ".dockerignore")


def stage(output):
    here = Path(__file__).resolve().parent
    root = here.parents[1]
    package = root / "src" / "foundry_distillation_lab"
    output = Path(output).resolve()
    if output == root or output in root.parents or here == output or here in output.parents:
        raise ValueError("Choose a new runs build-context directory, not source/template directories")
    output.mkdir(parents=True, exist_ok=False)
    files = [(here / name, Path(name)) for name in TEMPLATE_FILES]
    files.append((root / "LICENSE", Path("LICENSE")))
    # Only the runtime and journal are staged, never datasets or evaluation answers.
    files.extend((package / name, Path("foundry_distillation_lab") / name)
                 for name in ("__init__.py", "io.py", "safety.py"))
    files.extend((package / "retail" / name, Path("foundry_distillation_lab") / "retail" / name)
                 for name in RETAIL_FILES)
    hashes = {}
    for source, relative in files:
        if source.is_symlink() or not source.is_file():
            raise ValueError("Missing or linked staging source")
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        hashes[str(relative)] = hashlib.sha256(destination.read_bytes()).hexdigest()
    (output / "staging-manifest.json").write_text(
        json.dumps({"files": hashes, "cloud_verified": False}, indent=2) + "\n", encoding="utf-8")
    return hashes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(stage(parser.parse_args().output), indent=2))
