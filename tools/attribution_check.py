"""Refuse AI attribution in commit messages; the record names people. Not shipped.

Tenet 4 of the watch (#149): no AI attribution in commits, MRs, or notes, because the
repository is going public and its history is part of what it publishes. Nothing in the
tree refused it, so a ``Co-Authored-By:`` trailer naming an assistant, a ``Generated
with`` footer, and session links got through from 2026-08-27 to 2026-10-17.

One pattern set, three entry points. ``.githooks/commit-msg`` runs ``--message FILE``
on the message git is about to record and refuses the commit. ``make check`` runs the
default mode over ``origin/main..HEAD`` so a message that bypassed the hook (a clone
that never ran ``make hooks``, or ``--no-verify``) is red at the gate before it is
pushed. ``--strip`` reads a message on stdin and writes it back without the offending
lines; it exists for rewriting the record once, not for the gate.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import TextIO

EXIT_CLEAN = 0
EXIT_ATTRIBUTED = 1

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "origin/main"

# Names that identify an assistant anywhere in a message. Kept to spellings that are not
# also ordinary words, so a subject about a serial console's cursor is not refused.
_ASSISTANTS = ("claude", "anthropic", "chatgpt", "openai", "copilot")
# Wider list, applied only inside an attribution trailer where no other reading exists.
_TRAILER_ASSISTANTS = (
    *_ASSISTANTS,
    "gemini",
    "codex",
    "cursor",
    "devin",
    "aider",
    "windsurf",
    "ai assistant",
)
_ASSISTANT_HOSTS = ("claude.ai", "anthropic.com", "openai.com", "chatgpt.com")

_PATTERNS = (
    # Anywhere in a line: a mention by name.
    re.compile(rf"\b(?:{'|'.join(map(re.escape, _ASSISTANTS))})\b", re.IGNORECASE),
    # An attribution trailer naming any assistant, including the ambiguous names.
    re.compile(
        r"^\s*(?:co-authored-by|signed-off-by|reviewed-by|authored-by|assisted-by)\s*:.*"
        rf"(?:{'|'.join(map(re.escape, _TRAILER_ASSISTANTS))})",
        re.IGNORECASE,
    ),
    # A session trailer, whichever tool wrote it.
    re.compile(r"^\s*[\w-]*session\s*:\s*https?://", re.IGNORECASE),
    # A link into an assistant's own site.
    re.compile(rf"https?://[^\s)]*(?:{'|'.join(map(re.escape, _ASSISTANT_HOSTS))})", re.IGNORECASE),
)
_COMMENT = "#"


def offending_lines(message: str) -> tuple[str, ...]:
    """The lines of ``message`` that credit an assistant, in order; empty when clean.

    Lines starting with ``#`` are git's own commentary in the editor and are not part of
    the message that gets recorded, so they are skipped.
    """
    found: list[str] = []
    for line in message.splitlines():
        if line.startswith(_COMMENT):
            continue
        if any(pattern.search(line) for pattern in _PATTERNS):
            found.append(line)
    return tuple(found)


def strip(message: str) -> str:
    """``message`` without its offending lines and without the blank run they leave behind.

    Everything else is returned byte for byte, so a rewrite that uses this changes no
    message it did not have to.
    """
    offending = set(offending_lines(message))
    kept = [line for line in message.splitlines() if line not in offending]
    while kept and not kept[-1].strip():
        kept.pop()
    text = "\n".join(kept)
    return text + "\n" if message.endswith("\n") and text else text


def check_message(path: Path, out: TextIO = sys.stdout) -> int:
    """The hook: refuse the message at ``path`` when any line credits an assistant."""
    offending = offending_lines(path.read_text(encoding="utf-8"))
    if not offending:
        return EXIT_CLEAN
    print(
        "commit refused: the record names people, not assistants (STANDARDS.md section 7)", file=out
    )
    for line in offending:
        print(f"  {line}", file=out)
    return EXIT_ATTRIBUTED


def check_range(base: str = DEFAULT_BASE, out: TextIO = sys.stdout, cwd: Path = ROOT) -> int:
    """The gate: scan every commit in ``base..HEAD``, or all of ``HEAD`` when ``base`` is absent."""
    spec = f"{base}..HEAD" if _ref_exists(base, cwd) else "HEAD"
    commits = _commits(spec, cwd)
    attributed = [(sha, subject, offending_lines(body)) for sha, subject, body in commits]
    attributed = [entry for entry in attributed if entry[2]]
    print(
        f"attribution check: {len(commits)} commits in {spec}, {len(attributed)} attributed",
        file=out,
    )
    for sha, subject, lines in attributed:
        print(f"  {sha[:7]} {subject}", file=out)
        for line in lines:
            print(f"    {line}", file=out)
    if attributed:
        print(
            "AI attribution in the record: reword the commits above (STANDARDS.md section 7)",
            file=out,
        )
        return EXIT_ATTRIBUTED
    return EXIT_CLEAN


def _ref_exists(ref: str, cwd: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _commits(spec: str, cwd: Path) -> tuple[tuple[str, str, str], ...]:
    """``(sha, subject, full message)`` for every commit in ``spec``, oldest first."""
    result = subprocess.run(
        ["git", "log", "--reverse", "--format=%H%x00%s%x00%B%x01", spec],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    records = [record for record in result.stdout.split("\x01") if record.strip()]
    out: list[tuple[str, str, str]] = []
    for record in records:
        sha, subject, body = record.lstrip("\n").split("\x00", 2)
        out.append((sha, subject, body))
    return tuple(out)


def main(argv: list[str] | None = None) -> int:
    """Entry point: ``--message FILE`` (hook), ``--strip`` (stdin to stdout), or a range base."""
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--message"]:
        return check_message(Path(args[1]))
    if args[:1] == ["--strip"]:
        sys.stdout.write(strip(sys.stdin.read()))
        return EXIT_CLEAN
    return check_range(args[0] if args else DEFAULT_BASE)


if __name__ == "__main__":
    sys.exit(main())
