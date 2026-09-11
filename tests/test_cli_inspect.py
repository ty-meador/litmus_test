"""``litmus inspect``: what it opens, how it finds the latest run, and what it refuses.

Every test records a real run and then forbids suite execution before the command is
invoked, so a passing test is also the proof that inspection runs no suite code. The
Textual app is replaced by a recorder: the screen has its own tests, and what this
module holds to account is the path from the command line to the saved run it opens.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import time
from pathlib import Path

import pytest
from tests.inspector_rack import record_mixed_run

from litmus import LitmusTest
from litmus.cli import (
    EXIT_PASS,
    EXIT_USAGE,
    LitmusCommand,
    SavedRunLocator,
    SuiteLoader,
    UsageError,
)
from litmus.inspector_rerun import RerunSession
from litmus.inspector_run import SavedRun


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = LitmusCommand(stdout=out, stderr=err).run(argv)
    return status, out.getvalue(), err.getvalue()


def _record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Record the mixed run under ``tmp_path`` and return its run root."""
    saved = asyncio.run(record_mixed_run(tmp_path, monkeypatch))
    return saved.run_root


class _Opened:
    """What the command handed the screen: the saved runs, and the Re-Run sessions."""

    def __init__(self) -> None:
        self.runs: list[SavedRun] = []
        self.sessions: list[RerunSession] = []

    @property
    def roots(self) -> list[Path]:
        return [saved.run_root for saved in self.runs]


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> _Opened:
    """Replace the Textual app with a recorder of the runs the command hands it."""
    recorded = _Opened()

    class _Recorder:
        def __init__(self, saved: SavedRun, session: RerunSession) -> None:
            self._saved = saved
            recorded.sessions.append(session)

        def run(self) -> None:
            recorded.runs.append(self._saved)

    monkeypatch.setattr("litmus.cli.InspectorApp", _Recorder)
    return recorded


_SUITE = """
from litmus import PASS, LitmusTest, TestUtils, Verdict


class Bench(LitmusTest):
    def build_suite(self) -> LitmusTest:
        return self.test("claim", self._claim)

    async def _claim(self, utils: TestUtils) -> Verdict:
        return PASS
"""


class TestOpening:
    """A selected result, or a selected run directory, opens exactly that run."""

    def test_result_path_opens_that_run_without_suite_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: _Opened
    ) -> None:
        run_root = _record(tmp_path, monkeypatch)

        status, out, err = _run("inspect", str(run_root / "result.json"))

        assert (status, out, err) == (EXIT_PASS, "", "")
        assert opened.roots == [run_root.resolve()]

    def test_run_directory_selects_its_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: _Opened
    ) -> None:
        run_root = _record(tmp_path, monkeypatch)

        status, _, err = _run("inspect", str(run_root))

        assert (status, err) == (EXIT_PASS, "")
        assert opened.runs[0].run_root == run_root.resolve()
        assert opened.runs[0].tree.rows[0].ancestor_names == ("rack",)

    def test_help_names_path_and_the_root(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            _run("inspect", "--help")

        assert exit_info.value.code == 0
        text = capsys.readouterr().out
        assert "PATH" in text
        assert "--artifact-root" in text
        assert "LITMUS_ARTIFACT_ROOT" in text
        assert "--suite" in text


class TestRerunSuite:
    """Where the Re-Run tab's suite comes from, and what is refused at the prompt."""

    def test_a_run_without_a_recorded_target_leaves_the_tab_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: _Opened
    ) -> None:
        run_root = _record(tmp_path, monkeypatch)

        status, _, _ = _run("inspect", str(run_root))

        assert status == EXIT_PASS
        (session,) = opened.sessions
        assert session.available is False
        assert session.unavailable is not None
        assert "did not record the suite" in session.unavailable
        assert "--suite" in session.unavailable

    def test_the_recorded_target_is_loaded_only_when_the_tab_asks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: _Opened
    ) -> None:
        suite = tmp_path / "bench.py"
        suite.write_text(_SUITE, encoding="utf-8")
        root = tmp_path / "evidence"
        assert _run("run", "--artifact-root", str(root), str(suite))[0] == EXIT_PASS

        def refuse_load(self: SuiteLoader, target: str) -> type[LitmusTest]:
            raise AssertionError("loaded at open")

        monkeypatch.setattr(SuiteLoader, "load", refuse_load)

        status, _, _ = _run("inspect", "--artifact-root", str(root))

        assert status == EXIT_PASS
        (session,) = opened.sessions
        assert session.available is False
        monkeypatch.undo()
        asyncio.run(session.prepare())
        assert session.available is True, session.unavailable

    def test_an_explicit_suite_that_cannot_load_is_a_usage_error_at_the_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: _Opened
    ) -> None:
        run_root = _record(tmp_path, monkeypatch)

        status, _, err = _run("inspect", str(run_root), "--suite", str(tmp_path / "gone.py"))

        assert (status, opened.runs) == (EXIT_USAGE, [])
        assert err.startswith("litmus inspect: error: ")
        assert "no such file" in err

    def test_an_explicit_suite_of_another_shape_is_refused_on_the_tab(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: _Opened
    ) -> None:
        run_root = _record(tmp_path, monkeypatch)
        suite = tmp_path / "bench.py"
        suite.write_text(_SUITE, encoding="utf-8")

        status, _, err = _run("inspect", str(run_root), "--suite", str(suite))

        assert (status, err) == (EXIT_PASS, "")
        (session,) = opened.sessions
        asyncio.run(session.prepare())
        assert session.available is False
        assert session.unavailable is not None
        assert session.unavailable.startswith("the supplied suite differs from the saved run")


class TestLatestRun:
    """Without PATH, the most recently modified result.json under the root opens."""

    @staticmethod
    def _two_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
        """One recorded run and a copy of it, the copy's result the newer one."""
        (tmp_path / "first").mkdir()
        older = _record(tmp_path / "first", monkeypatch)
        newer = tmp_path / "second" / "mixed"
        shutil.copytree(older, newer)
        now = time.time()
        os.utime(older / "result.json", (now - 600, now - 600))
        os.utime(newer / "result.json", (now, now))
        return older, newer

    def test_latest_result_under_the_environment_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: _Opened
    ) -> None:
        _, newer = self._two_runs(tmp_path, monkeypatch)
        monkeypatch.setenv("LITMUS_ARTIFACT_ROOT", str(tmp_path))

        status, _, err = _run("inspect")

        assert (status, err) == (EXIT_PASS, "")
        assert opened.roots == [newer.resolve()]

    def test_the_flag_overrides_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: _Opened
    ) -> None:
        older, newer = self._two_runs(tmp_path, monkeypatch)
        monkeypatch.setenv("LITMUS_ARTIFACT_ROOT", str(tmp_path / "second"))

        status, _, err = _run("inspect", "--artifact-root", str(tmp_path / "first"))

        assert (status, err) == (EXIT_PASS, "")
        assert opened.roots == [older.resolve()]
        assert newer.resolve() not in opened.roots

    def test_the_default_root_is_artifacts_in_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: _Opened
    ) -> None:
        run_root = _record(tmp_path, monkeypatch)
        shutil.move(run_root, tmp_path / "artifacts" / "kept")
        monkeypatch.delenv("LITMUS_ARTIFACT_ROOT")
        monkeypatch.chdir(tmp_path)

        status, _, err = _run("inspect")

        assert (status, err) == (EXIT_PASS, "")
        assert opened.runs[0].run_root == (tmp_path / "artifacts" / "kept").resolve()

    def test_locator_ignores_a_directory_named_like_the_result(self, tmp_path: Path) -> None:
        (tmp_path / "decoy" / "result.json").mkdir(parents=True)
        real = tmp_path / "run" / "result.json"
        real.parent.mkdir()
        real.write_text("{}", encoding="utf-8")

        assert SavedRunLocator().locate(None, artifact_root=tmp_path) == real


class TestRefusals:
    """Nothing to open is exit 3 with the reason; no run is started to manufacture one."""

    def test_nothing_under_the_root_names_the_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opened: _Opened
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("LITMUS_ARTIFACT_ROOT", str(empty))

        status, out, err = _run("inspect")

        assert (status, out, opened.runs) == (EXIT_USAGE, "", [])
        assert err.startswith("litmus inspect: error: no saved run under ")
        assert str(empty) in err
        assert "nothing to inspect" in err

    def test_an_absent_root_is_the_same_refusal(self, tmp_path: Path) -> None:
        with pytest.raises(UsageError, match="no saved run under"):
            SavedRunLocator().locate(None, artifact_root=tmp_path / "never")

    def test_a_missing_selection_carries_the_reader_diagnostic(
        self, tmp_path: Path, opened: _Opened
    ) -> None:
        absent = tmp_path / "gone" / "result.json"

        status, _, err = _run("inspect", str(absent))

        assert (status, opened.runs) == (EXIT_USAGE, [])
        assert err.startswith("litmus inspect: error: ")
        assert str(absent) in err
        assert "unavailable" in err

    def test_an_unsupported_version_is_refused_not_guessed(
        self, tmp_path: Path, opened: _Opened
    ) -> None:
        result = tmp_path / "result.json"
        result.write_text('{"format_version": 2}', encoding="utf-8")

        status, _, err = _run("inspect", str(tmp_path))

        assert (status, opened.runs) == (EXIT_USAGE, [])
        assert "format_version 2" in err
        assert "reader accepts integer 1" in err

    def test_a_file_that_is_not_a_record_is_refused(self, tmp_path: Path, opened: _Opened) -> None:
        result = tmp_path / "result.json"
        result.write_text("not json\n", encoding="utf-8")

        status, _, err = _run("inspect", str(result))

        assert (status, opened.runs) == (EXIT_USAGE, [])
        assert err.startswith("litmus inspect: error: ")
        assert str(result) in err
