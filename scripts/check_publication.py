"""Check the curated public tree; this is not a comprehensive secret or license audit."""

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {"src", "scripts", "configs", "data", "examples", "docs", "tests", "deploy", ".github"}
EXCLUDED = {"__pycache__", "local", ".azure", ".approval-state"}
ROOT_FILES = {"README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "pyproject.toml",
              ".gitignore", ".gitattributes"}


def public_files():
    for name in sorted(ROOT_FILES):
        path = ROOT / name
        if not path.is_file():
            raise ValueError(f"Missing public root file: {name}")
        yield path
    for name in sorted(ALLOWED):
        for path in sorted((ROOT / name).rglob("*")):
            relative = path.relative_to(ROOT)
            if any(part in EXCLUDED or part.endswith(".egg-info") for part in relative.parts):
                continue
            if path.is_file():
                yield path


def main():
    problems, count = [], 0
    for path in public_files():
        count += 1
        relative = path.relative_to(ROOT)
        if path.name.startswith(".env") and path.name != ".env.example":
            problems.append(f"{relative}: environment file")
        text = path.read_text(encoding="utf-8-sig")
        if str(ROOT.parent).casefold() in text.casefold():
            problems.append(f"{relative}: machine-local absolute path")
        if re.search(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{24,}\b", text):
            problems.append(f"{relative}: credential-like token")
        if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", text):
            problems.append(f"{relative}: private key")
        if path.suffix == ".md":
            for link in re.findall(r"(?<!!)\[[^\]]+\]\(([^)\s]+)\)", text):
                if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", link) or link.startswith("#"):
                    continue
                target = link.split("#", 1)[0].replace("/", "\\")
                if target and not (path.parent / target).exists():
                    problems.append(f"{relative}: broken relative link {link}")
    if problems:
        raise SystemExit("\n".join(problems))
    print(f"Curated public file checks passed: {count} files. Manual publication review still required.")


if __name__ == "__main__":
    main()
