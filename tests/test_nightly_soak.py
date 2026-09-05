"""The nightly soak driver: failure parsing, the night's verdict, and what it posts where."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from tools.nightly_soak import (
    GREEN_STREAK_TARGET,
    ISSUE_IID,
    NightlySoak,
    NightReport,
    SoakRun,
)

RACK = "tests/test_rack_scale.py"
CI_ENV: Mapping[str, str] = {
    "CI_API_V4_URL": "https://gitlab.example/api/v4",
    "CI_PROJECT_ID": "85833370",
    "CI_PIPELINE_URL": "https://gitlab.example/p/1",
    "CI_COMMIT_SHORT_SHA": "abc1234",
    "LITMUS_BOT_TOKEN": "glpat-test",
    "DISCORD_WEBHOOK_URL": "https://discord.example/hook",
    "SOAK_RUNS": "2",
    "RACK_REPEATS": "3",
}


class _FakeRunner:
    """Scripted pytest outcomes keyed by (series target, run number); green by default."""

    def __init__(self, red: Mapping[tuple[str, int], str]) -> None:
        self._red = dict(red)
        self._seen: dict[str, int] = {}
        self.argv: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.argv.append(list(argv))
        target = argv[-1] if argv[-1].startswith("tests/") else "all"
        number = self._seen.get(target, 0) + 1
        self._seen[target] = number
        stdout = self._red.get((target, number))
        if stdout is None:
            return subprocess.CompletedProcess(list(argv), 0, "..\n2 passed\n", "")
        return subprocess.CompletedProcess(list(argv), 1, stdout, "")


class _FakeHttp:
    """Records every request; answers the notes GET with a scripted note list."""

    def __init__(self, notes: Sequence[str] = ()) -> None:
        self.calls: list[tuple[str, str, Mapping[str, str], object | None]] = []
        self._notes = [{"body": body} for body in notes]

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: object | None
    ) -> object:
        self.calls.append((method, url, headers, body))
        if method == "GET":
            return self._notes
        return {"id": 1}

    def posts_to(self, fragment: str) -> list[object]:
        return [body for method, url, _, body in self.calls if method == "POST" and fragment in url]


def _green_marker(sha: str = "aaa") -> str:
    return f"green night\n\n<!-- nightly-soak verdict=green sha={sha} -->"


RED_MARKER = "red night\n\n<!-- nightly-soak verdict=red sha=bbb -->"
RACK_FAILURE = (
    "F.\n"
    "=== short test summary info ===\n"
    f"FAILED {RACK}::TestRackScale::test_whole_rack_abort_latency - AssertionError\n"
    "1 failed, 1 passed\n"
)


def _soak(
    runner: _FakeRunner,
    http: _FakeHttp,
    tmp_path: Path,
    env: Mapping[str, str] = CI_ENV,
    *,
    post: bool = True,
) -> tuple[NightlySoak, io.StringIO]:
    out = io.StringIO()
    soak = NightlySoak(env=env, post=post, runner=runner, http=http, log_dir=tmp_path, out=out)
    return soak, out


class TestSoakRun:
    def test_green_needs_exit_zero_and_no_named_failure(self) -> None:
        assert SoakRun("soak", 1, 8, 0, ()).green
        assert not SoakRun("soak", 1, 8, 1, ()).green
        assert not SoakRun("soak", 1, 8, 0, ("tests/test_x.py::T::t",)).green

    def test_describe_names_run_number_and_cases_or_the_bare_exit(self) -> None:
        named = SoakRun("rack", 7, 20, 1, ("tests/a.py::T::x", "tests/a.py::T::y"))
        bare = SoakRun("soak", 2, 8, 2, ())

        assert named.describe() == "rack run 7/20: tests/a.py::T::x, tests/a.py::T::y"
        assert bare.describe() == "soak run 2/8: exit 2, no case named"


class TestNightReport:
    def _report(self, *runs: SoakRun) -> NightReport:
        return NightReport(runs, sha="abc1234", pipeline_url="https://p/1", date="2026-09-05")

    def test_tally_counts_green_over_total_per_series_in_order(self) -> None:
        report = self._report(
            SoakRun("soak", 1, 2, 0, ()),
            SoakRun("soak", 2, 2, 1, ("tests/a.py::T::t",)),
            SoakRun("rack", 1, 1, 0, ()),
        )

        assert report.tally() == "soak 1/2, rack 1/1"
        assert not report.green
        assert report.headline() == "Nightly soak RED 2026-09-05 at abc1234: soak 1/2, rack 1/1"

    def test_green_note_carries_the_streak_and_a_green_marker(self) -> None:
        note = self._report(SoakRun("soak", 1, 1, 0, ())).issue_note(streak=5)

        assert f"Green night 5 of {GREEN_STREAK_TARGET} consecutive" in note
        assert "<!-- nightly-soak verdict=green sha=abc1234 -->" in note
        assert "https://p/1" in note

    def test_red_note_names_every_failing_run_and_resets_the_streak(self) -> None:
        report = self._report(
            SoakRun("soak", 3, 8, 1, ("tests/a.py::T::t",)),
            SoakRun("rack", 7, 20, 1, ("tests/b.py::R::r",)),
        )
        note = report.issue_note(streak=0)

        assert "Streak reset" in note
        assert "- soak run 3/8: tests/a.py::T::t" in note
        assert "- rack run 7/20: tests/b.py::R::r" in note
        assert "<!-- nightly-soak verdict=red sha=abc1234 -->" in note

    def test_discord_message_stays_under_the_limit(self) -> None:
        many = tuple(
            SoakRun("rack", n, 200, 1, (f"tests/test_rack_scale.py::TestRack::test_{n:03d}",))
            for n in range(1, 201)
        )
        message = self._report(*many).discord_message()

        assert len(message) <= 1900
        assert message.startswith("Nightly soak RED")
        assert message.endswith("…")


class TestNightlySoak:
    def test_runs_both_series_the_configured_number_of_times(self, tmp_path: Path) -> None:
        runner, http = _FakeRunner({}), _FakeHttp()
        soak, _ = _soak(runner, http, tmp_path)

        assert soak.run() == 0
        full = [argv for argv in runner.argv if argv[-1] == "-rfE"]
        rack = [argv for argv in runner.argv if argv[-1] == RACK]
        assert (len(full), len(rack)) == (2, 3)
        assert full[0] == ["uv", "run", "pytest", "-q", "-rfE"]
        assert sorted(path.name for path in tmp_path.iterdir()) == [
            "rack-01.log",
            "rack-02.log",
            "rack-03.log",
            "soak-01.log",
            "soak-02.log",
        ]

    def test_defaults_are_eight_soak_runs_and_twenty_rack_repeats(self, tmp_path: Path) -> None:
        runner, http = _FakeRunner({}), _FakeHttp()
        env = {key: value for key, value in CI_ENV.items() if not key.endswith(("RUNS", "REPEATS"))}
        soak, _ = _soak(runner, http, tmp_path, env)

        soak.run()

        assert len(runner.argv) == 28

    def test_a_run_count_below_one_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="SOAK_RUNS must be at least 1"):
            _soak(_FakeRunner({}), _FakeHttp(), tmp_path, {**CI_ENV, "SOAK_RUNS": "0"})

    def test_green_night_extends_the_streak_on_the_issue_and_stays_off_discord(
        self, tmp_path: Path
    ) -> None:
        notes = ["Ty: looking good", _green_marker(), _green_marker(), RED_MARKER, _green_marker()]
        runner, http = _FakeRunner({}), _FakeHttp(notes)
        soak, out = _soak(runner, http, tmp_path)

        assert soak.run() == 0
        [note] = http.posts_to(f"issues/{ISSUE_IID}/notes")
        body = json.dumps(note)
        assert f"Green night 3 of {GREEN_STREAK_TARGET} consecutive" in body
        assert "soak 2/2, rack 3/3" in body
        assert http.posts_to("discord.example") == []
        assert f"recorded on #{ISSUE_IID}" in out.getvalue()

    def test_red_night_names_the_case_and_run_on_the_issue_and_in_general(
        self, tmp_path: Path
    ) -> None:
        runner = _FakeRunner({(RACK, 2): RACK_FAILURE})
        http = _FakeHttp([_green_marker()])
        soak, out = _soak(runner, http, tmp_path)

        assert soak.run() == 1
        [note] = http.posts_to(f"issues/{ISSUE_IID}/notes")
        [post] = http.posts_to("discord.example")
        expected = f"rack run 2/3: {RACK}::TestRackScale::test_whole_rack_abort_latency"
        assert expected in json.dumps(note)
        assert expected in json.dumps(post)
        assert "Streak reset" in json.dumps(note)
        assert [call[0] for call in http.calls] == ["POST", "POST"]
        assert "posted to #general" in out.getvalue()
        assert expected in out.getvalue()

    def test_every_run_of_a_series_executes_after_a_failure(self, tmp_path: Path) -> None:
        runner = _FakeRunner({("all", 1): "F\nFAILED tests/a.py::T::t - boom\n1 failed\n"})
        soak, out = _soak(runner, _FakeHttp(), tmp_path)

        assert soak.run() == 1
        assert len(runner.argv) == 5
        assert "soak 1/2, rack 3/3" in out.getvalue()

    def test_error_lines_count_as_failures_and_repeats_collapse(self, tmp_path: Path) -> None:
        stdout = (
            "EE\n"
            "ERROR tests/a.py::T::t - fixture blew up\n"
            "ERROR tests/a.py::T::t - fixture blew up\n"
            "FAILED tests/a.py::T::u - assert\n"
            "1 failed, 2 errors\n"
        )
        runner = _FakeRunner({("all", 2): stdout})
        soak, out = _soak(runner, _FakeHttp(), tmp_path)

        soak.run()

        assert "soak run 2/2: tests/a.py::T::t, tests/a.py::T::u" in out.getvalue()

    def test_missing_secrets_are_reported_and_the_verdict_still_decides(
        self, tmp_path: Path
    ) -> None:
        runner = _FakeRunner({(RACK, 1): RACK_FAILURE})
        http = _FakeHttp()
        env = {
            k: v for k, v in CI_ENV.items() if k not in {"LITMUS_BOT_TOKEN", "DISCORD_WEBHOOK_URL"}
        }
        soak, out = _soak(runner, http, tmp_path, env)

        assert soak.run() == 1
        assert http.calls == []
        assert f"LITMUS_BOT_TOKEN unset; night not recorded on #{ISSUE_IID}" in out.getvalue()
        assert "DISCORD_WEBHOOK_URL unset; red night not posted to #general" in out.getvalue()

    def test_no_post_at_the_bench_runs_everything_and_touches_nothing(self, tmp_path: Path) -> None:
        runner, http = _FakeRunner({}), _FakeHttp()
        bench = {"SOAK_RUNS": "2", "RACK_REPEATS": "3"}
        soak, out = _soak(runner, http, tmp_path, bench, post=False)

        assert soak.run() == 0
        assert http.calls == []
        assert "posting disabled; nothing recorded" in out.getvalue()
        assert "at local: soak 2/2, rack 3/3" in out.getvalue()
