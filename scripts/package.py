#!/usr/bin/env python3
"""Purpose: build a reproducible, self-contained skill archive without private runtime artifacts."""

import argparse
import gzip
import hashlib
import io
import json
import tarfile
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ("agents", "references", "scripts", "assets", "tests")
# Keep exclusions aligned with .gitignore; portable archives cannot rely on Git being installed.
EXCLUDED_DIRECTORIES = {
    ".git",
    ".idea",
    ".vscode",
    ".codex",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".nox",
    "htmlcov",
    "build",
    "dist",
    ".local",
    ".state",
    ".terraform",
}
EXCLUDED_FILES = (
    "*.iml",
    ".DS_Store",
    "*.swp",
    "*~",
    ".python-version",
    "uv.lock",
    "*.py[cod]",
    ".coverage",
    ".coverage.*",
    "coverage.xml",
    "*.tmp",
    "*.tfstate*",
    "*.tfplan*",
    "*.tfvars",
    "*.tfvars.json",
    "*.log",
    ".env",
    ".env.*",
    "kubeconfig",
    "*.kubeconfig",
    "*.pem",
    "*.key",
    "*.crt",
    "*.csr",
    "*.srl",
)


# Inputs: source skill directory. Outputs: relative file paths with bytes.
# Purpose: collect only maintained distributable files. Dependencies: explicit directory allowlist.
def sources(root):
    paths = [root / name for name in ("SKILL.md", "AGENTS.md", "pyproject.toml")]
    for directory in DIRECTORIES:
        paths.extend(path for path in (root / directory).rglob("*") if path.is_file())
    result = {}
    for path in sorted(paths):
        relative = path.relative_to(root)
        if any(
            part in EXCLUDED_DIRECTORIES or fnmatchcase(part, "*.egg-info")
            for part in relative.parts[:-1]
        ):
            continue
        if path.name != ".env.example" and any(
            fnmatchcase(path.name, pattern) for pattern in EXCLUDED_FILES
        ):
            continue
        if path.is_symlink():
            raise ValueError("Distributable assets must not be symbolic links")
        result[relative.as_posix()] = path.read_bytes()
    return result


# Inputs: source root and archive path. Outputs: archive checksum and file count.
# Purpose: create a repeatable package with an internal source manifest. Dependencies: gzip and tarfile.
def build(root, output):
    entries = sources(root)
    manifest = {
        "$comment": "Purpose: verify exact packaged skill source contents; runtime acceptance is not implied.",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in entries.items()},
    }
    entries["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with (
        output.open("wb") as stream,
        gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w") as archive,
    ):
        for name, data in sorted(entries.items()):
            info = tarfile.TarInfo(f"local-k8s-platform/{name}")
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            archive.addfile(info, io.BytesIO(data))
    return {
        "archive": str(output.resolve()),
        "files": len(entries),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


# Inputs: CLI output argument. Outputs: none. Purpose: expose the offline package builder.
# Dependencies: argparse and build; no package installation or network access.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(ROOT, args.output), indent=2))


if __name__ == "__main__":
    main()
