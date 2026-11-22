#!/usr/bin/env python3
"""Reject direct ``gh auth token`` callers unless their login fallback is documented.

GitHub App tokens are the default for reads.  A small number of write paths still
need the host login; those paths carry an explicit marker immediately above the
call so a new login consumer cannot slip into the tree unnoticed.
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

# Only the audited call sites may keep a local login probe or authored-write source.
# Copying an exception comment into a new tool must not grant it one.
APPROVED = {'scripts/glaeda-gh': 'authored-write', 'scripts/glaeda-update': 'presence-only'}
MARKER = "gh-auth-token-lint: allow"
SKIP_SUFFIXES = {".md", ".rst", ".txt", ".json", ".lock", ".svg", ".png", ".jpg"}


def allowed(path, lines: list[str], line: int) -> bool:
    reason = APPROVED.get(str(path))
    return bool(reason and line > 0 and lines[line - 1].lstrip().startswith(f"# {MARKER} {reason}:"))


def literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return node.id
    return None


def python_hits(path: Path, text: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []
    lines = re.split(r"\r\n|\r|\n", text)
    hits: list[tuple[int, str]] = []
    # A comparison against a mocked argv or a table of command prefixes does not
    # execute anything. Inspect command arguments and directly assigned argv.
    command_args = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            command_args.update(id(arg) for arg in node.args)
            command_args.update(id(kw.value) for kw in node.keywords if kw.arg == "args")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            command_args.add(id(node.value))
    for node in ast.walk(tree):
        if id(node) in command_args and isinstance(node, (ast.List, ast.Tuple)):
            values = [literal(item) for item in node.elts]
            first = (values[0] if values else "") or ""
            if len(values) >= 3 and values[1:3] == ["auth", "token"] and (
                    first in {"gh", "gh_path", "gh_bin"} or first.endswith("/gh")):
                if not allowed(path, lines, node.lineno - 1):
                    hits.append((node.lineno, "Python argv invokes gh auth token"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
                "run", "Popen", "system", "popen", "getoutput", "getstatusoutput", "check_output", "check_call", "call"):
            arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "args"), None)
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and SHELL_TOKEN.search(arg.value):
                if not allowed(path, lines, node.lineno - 1):
                    hits.append((node.lineno, "Python shell command invokes gh auth token"))
    return hits


SHELL_TOKEN = re.compile(r"(?<![\w-])gh[ \t]+auth[ \t]+token\b")


def shell_hits(path: Path, text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    lines = re.split(r"\r\n|\r|\n", text)
    for number, raw in enumerate(lines, 1):
        code = raw.split("#", 1)[0]
        if "gh" not in code or "auth" not in code or "token" not in code:
            continue
        if SHELL_TOKEN.search(code) and not allowed(path, lines, number - 1):
            hits.append((number, "Shell command invokes gh auth token"))
    return hits


def tracked(root: Path) -> list[Path]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
    return [root / item for item in result.stdout.decode().split("\0") if item]


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    for path in tracked(root):
        if path.is_symlink() or path.suffix.lower() in SKIP_SUFFIXES or path == root / "scripts/gh_auth_token_lint.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not all(word in text for word in ("gh", "auth", "token")):
            continue
        first, _, rest = text.partition("\n")
        # a /bin/sh first line that execs python on the second (glaeda-gh's launcher) is a Python file too
        is_python = (path.suffix == ".py" or text.startswith("#!") and "python" in first
                     or first == "#!/bin/sh" and rest.startswith('"""exec" python'))
        rel = path.relative_to(root)
        hits = python_hits(rel, text) if is_python else shell_hits(rel, text)
        findings.extend(f"{path.relative_to(root)}:{line}: {why}" for line, why in hits)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args()
    findings = scan(args.root.resolve())
    if findings:
        print("gh_auth_token_lint: direct login-token callers are not allowed:", file=sys.stderr)
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("gh_auth_token_lint: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
