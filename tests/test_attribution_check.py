"""The attribution gate: red on a commit message that credits an assistant, green otherwise.

Messages are checked as strings so the contract is pinned against text the test controls;
the range scan is exercised against a repository the test writes itself, the way
``tools/import_cycles.py`` is exercised against a seeded package.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from tools.attribution_check import (
    EXIT_ATTRIBUTED,
    EXIT_CLEAN,
    check_message,
    check_range,
    main,
    offending_lines,
    strip,
)

CLEAN = (
    "Wake the serial line before the first expect (#139)\n"
    "\n"
    "A console already at its prompt never re-prompted, so configure() timed out.\n"
    "The cursor sits at the shell after the drain; the bot token is unaffected.\n"
)
TRAILER = "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
FOOTER = "Generated with [Claude Code](https://claude.ai/code)"
SESSION = "Claude-Session: https://claude.ai/code/session_01HsC2dcLgwndHmvY6EmvQJo"


class TestOffendingLines:
    def test_a_clean_message_has_none(self) -> None:
        assert offending_lines(CLEAN) == ()

    @pytest.mark.parametrize("line", [TRAILER, FOOTER, SESSION])
    def test_each_attribution_shape_is_named(self, line: str) -> None:
        assert offending_lines(CLEAN + "\n" + line + "\n") == (line,)

    def test_a_trailer_naming_any_assistant_is_refused(self) -> None:
        line = "co-authored-by: Gemini <gemini@example.com>"
        assert offending_lines(f"Subject\n\n{line}\n") == (line,)

    def test_a_mention_in_the_body_is_refused_case_insensitively(self) -> None:
        line = "Drafted with ANTHROPIC's assistant and reviewed by hand."
        assert offending_lines(f"Subject\n\n{line}\n") == (line,)

    def test_editor_commentary_is_not_the_message(self) -> None:
        message = CLEAN + "# Please enter the commit message. Lines starting with '#'\n# claude\n"
        assert offending_lines(message) == ()


class TestStrip:
    def test_a_clean_message_is_returned_byte_for_byte(self) -> None:
        assert strip(CLEAN) == CLEAN

    def test_offending_lines_go_and_the_blank_run_they_leave_goes_with_them(self) -> None:
        assert strip(CLEAN + "\n" + TRAILER + "\n\n" + SESSION + "\n") == CLEAN

    def test_a_subject_only_message_keeps_its_subject(self) -> None:
        assert strip("Subject\n\n" + TRAILER + "\n") == "Subject\n"


class TestCheckMessage:
    def test_the_hook_accepts_a_clean_message(self, tmp_path: Path) -> None:
        path = tmp_path / "COMMIT_EDITMSG"
        path.write_text(CLEAN, encoding="utf-8")
        out = io.StringIO()

        assert check_message(path, out) == EXIT_CLEAN
        assert out.getvalue() == ""

    def test_the_hook_refuses_and_names_the_line(self, tmp_path: Path) -> None:
        path = tmp_path / "COMMIT_EDITMSG"
        path.write_text(CLEAN + "\n" + TRAILER + "\n", encoding="utf-8")
        out = io.StringIO()

        assert main(["--message", str(path)]) == EXIT_ATTRIBUTED
        assert check_message(path, out) == EXIT_ATTRIBUTED
        assert TRAILER in out.getvalue()
        assert "STANDARDS.md section 7" in out.getvalue()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "Ikora Rey",
            "GIT_AUTHOR_EMAIL": "ikora@example.com",
            "GIT_COMMITTER_NAME": "Ikora Rey",
            "GIT_COMMITTER_EMAIL": "ikora@example.com",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin",
        },
    )


def _seed_repo(root: Path, messages: list[str]) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for number, message in enumerate(messages):
        (repo / f"file{number}").write_text(message, encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", message)
    return repo


class TestCheckRange:
    def test_a_clean_range_is_green(self, tmp_path: Path) -> None:
        repo = _seed_repo(tmp_path, [CLEAN, "Second commit\n"])
        _git(repo, "branch", "base", "HEAD~1")
        out = io.StringIO()

        assert check_range("base", out, repo) == EXIT_CLEAN
        assert out.getvalue() == "attribution check: 1 commits in base..HEAD, 0 attributed\n"

    def test_an_attributed_commit_is_red_and_named(self, tmp_path: Path) -> None:
        repo = _seed_repo(tmp_path, ["Base commit\n", CLEAN + "\n" + TRAILER + "\n"])
        _git(repo, "branch", "base", "HEAD~1")
        out = io.StringIO()

        assert check_range("base", out, repo) == EXIT_ATTRIBUTED
        assert "1 attributed" in out.getvalue()
        assert TRAILER in out.getvalue()

    def test_a_missing_base_scans_the_whole_history(self, tmp_path: Path) -> None:
        repo = _seed_repo(tmp_path, [SESSION + "\n", "Clean\n"])
        out = io.StringIO()

        assert check_range("origin/main", out, repo) == EXIT_ATTRIBUTED
        assert "2 commits in HEAD, 1 attributed" in out.getvalue()
