"""Secret scan before a push. Prints LOCATIONS ONLY, never a value or a part of one.

    .venv/bin/python scripts/secret_scan.py [--env-root DIR]

Scans every blob reachable from any ref of this repository, and the staged content, for:
  - the real values in `<env-root>/.env` and `<env-root>/data/api_token.txt` (substring match;
    `--env-root` defaults to the current folder: pass the main checkout when running from a
    worktree, which has no `.env`);
  - token shapes: a JWT, a Telegram bot token.
Exit code 0 when nothing is found beyond the known harmless hits in ALLOW, 1 otherwise, so a
`scan && push` chain stops. Never pipe this script into another command before `&&`: the
pipe hides the exit code.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: (path, kind) of hits that were looked at and are harmless
ALLOW = {("tests/test_invariants.py", "JWT-shaped")}  # a fake token the invariant test needs
SHAPES = {
    "JWT-shaped": re.compile(rb"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "Telegram-token-shaped": re.compile(rb"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
}
MIN_SECRET = 8  # shorter values (a port, a flag) would match by accident


def sh(*args: str) -> bytes:
    return subprocess.run(args, check=True, capture_output=True).stdout


def load_secrets(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    env = root / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                if len(value) >= MIN_SECRET:
                    out[key.strip()] = value.encode()
    token = root / "data" / "api_token.txt"
    if token.exists():
        value = token.read_text(encoding="utf-8", errors="replace").strip()
        if len(value) >= MIN_SECRET:
            out["api_token.txt"] = value.encode()
    return out


def hits_in(data: bytes, secrets: dict[str, bytes]) -> list[tuple[str, int]]:
    """(kind, line number) of every hit in one file's bytes."""
    found: list[tuple[str, int]] = []
    for name, value in secrets.items():
        start = data.find(value)
        while start != -1:
            found.append((f"value of {name}", data.count(b"\n", 0, start) + 1))
            start = data.find(value, start + 1)
    for kind, pattern in SHAPES.items():
        for m in pattern.finditer(data):
            found.append((kind, data.count(b"\n", 0, m.start()) + 1))
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env-root", default=".", help="folder holding .env and data/api_token.txt")
    args = ap.parse_args()
    secrets = load_secrets(Path(args.env_root))
    print(f"secret values loaded (names only): {sorted(secrets)}")
    if not secrets:
        print("WARNING: no secret values loaded; only token shapes are checked")

    findings: list[tuple[str, str, str, int]] = []  # (where, path, kind, line)
    seen: dict[str, str] = {}
    for line in sh("git", "rev-list", "--all", "--objects").decode(errors="replace").splitlines():
        sha, _, path = line.partition(" ")
        if path and sha not in seen:
            seen[sha] = path
    kinds = sh(
        "git", "cat-file", "--batch-check=%(objectname) %(objecttype)", "--batch-all-objects"
    )
    blobs = {ln.split()[0] for ln in kinds.decode().splitlines() if ln.endswith(" blob")} & set(
        seen
    )
    for sha in sorted(blobs):
        data = sh("git", "cat-file", "blob", sha)
        for kind, line_no in hits_in(data, secrets):
            findings.append((f"HISTORY blob {sha[:8]}", seen[sha], kind, line_no))
    staged = [p for p in sh("git", "diff", "--cached", "--name-only").decode().splitlines() if p]
    for path in staged:
        try:
            data = sh("git", "show", f":{path}")
        except subprocess.CalledProcessError:
            continue  # deleted in the index
        for kind, line_no in hits_in(data, secrets):
            findings.append(("STAGED", path, kind, line_no))

    new = 0
    for where, path, kind, line_no in findings:
        known = (path, kind) in ALLOW
        new += not known
        print(f"{'known ' if known else 'NEW   '}{where}  {kind}: {path} line {line_no}")
    print(f"history: {len(blobs)} blobs scanned; staged: {len(staged)} files scanned")
    print(f"RESULT: {new} new location(s), {len(findings) - new} known harmless")
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
