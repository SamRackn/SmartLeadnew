"""Refuse a commit whose staged additions look like a secret.

Run by `.githooks/pre-commit` (enable once: `git config core.hooksPath .githooks`).
Scans only added lines of the staged diff. Exit 1 names file and pattern,
never prints the matched value.
"""

from __future__ import annotations

import re
import subprocess
import sys

# Prefixes assembled from parts so this file never matches itself.
PATTERNS = {
    "anthropic key": re.compile("sk" + r"-ant-[A-Za-z0-9_\-]{20,}"),
    "openai-style key": re.compile("s" + r"k-[A-Za-z0-9]{32,}"),
    "google api key": re.compile("AI" + r"za[0-9A-Za-z_\-]{35}"),
    "google oauth secret": re.compile("GO" + r"CSPX-[0-9A-Za-z_\-]{20,}"),
    "google refresh token": re.compile(r"\b1//0[0-9A-Za-z_\-]{30,}"),
    "aws access key": re.compile("AK" + r"IA[0-9A-Z]{16}"),
    "private key block": re.compile("-----BEGIN " + r"[A-Z ]*PRIVATE KEY-----"),
    "service account json": re.compile(r'"private_key"\s*:\s*"-----'),
    "github token": re.compile("gh" + r"[pousr]_[A-Za-z0-9]{36,}"),
    "slack token": re.compile("xo" + r"x[abpr]-[A-Za-z0-9\-]{10,}"),
    "password in url": re.compile(r"://[^/\s:@]+:(?!\*\*\*)[^@\s/]{6,}@"),
}


def scan(diff: str) -> list[tuple[str, str]]:
    hits, path = [], "?"
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+"):
            hits += [(path, name) for name, rx in PATTERNS.items() if rx.search(line)]
    return hits


def main() -> int:
    diff = subprocess.run(
        ["git", "diff", "--cached", "-U0", "--no-color"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    hits = scan(diff)
    for path, name in hits:
        print(f"secret_scan: {path}: looks like a {name}", file=sys.stderr)
    if hits:
        print("secret_scan: commit refused. Move the value to env / the host secret store.", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
