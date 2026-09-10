"""Nightly soak: the full suite and the rack suite many times over, verdict posted. Not shipped.

Runs from the ``nightly-soak`` CI schedule (see .gitlab-ci.yml) on the ``main`` head.
``make soak`` exists because one green run proves nothing about timing, and nobody runs
it unprompted; this is the run nobody has to remember. It executes the full suite
``SOAK_RUNS`` times (default 8, as ``make soak``) and ``tests/test_rack_scale.py``
``RACK_REPEATS`` times (default 20), keeps every run's pytest output under ``.soak/``,
and records the night on issue #68: a green note carrying the consecutive-green streak
the Phase 6 exit counts, or a red note naming each failing run and case. A red night
also reaches #general through ``DISCORD_WEBHOOK_URL`` so the fireteam sees the failing
case before an MR does. Exit code 0 on a green night, 1 on red; every run of a series
executes even after a failure, because "3 of 8" is the datum a timing race leaves.

Each invocation draws a fresh Hypothesis seed and records its replay command before
starting pytest. This overrides the fixed gate seed without changing ``make check``.

Environment (CI provides all but the two secrets): ``LITMUS_BOT_TOKEN`` (api scope),
``DISCORD_WEBHOOK_URL``, ``CI_API_V4_URL``, ``CI_PROJECT_ID``, ``CI_PIPELINE_URL``,
``CI_COMMIT_SHORT_SHA``. A missing secret is reported, never fatal: the suite verdict
decides the exit code, and the pipeline stays the record of a night that could not be
posted. ``--no-post`` runs and prints only (``make nightly``).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / ".soak"
ISSUE_IID = 68
RACK_SUITE = "tests/test_rack_scale.py"
DEFAULT_SOAK_RUNS = 8
DEFAULT_RACK_REPEATS = 20
GREEN_STREAK_TARGET = 14
DISCORD_MESSAGE_LIMIT = 1900
_FAILURE_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
_MARKER = re.compile(r"<!-- nightly-soak verdict=(green|red) ")

type Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
type Http = Callable[[str, str, Mapping[str, str], object | None], object]


@dataclass(frozen=True)
class SoakRun:
    """One pytest invocation of a series: which series, its index, and what it failed.

    ``failed`` holds the node ids from pytest's short summary (``-rfE``), in order and
    without repeats. A run is green only when it exited 0 and named no failure.
    """

    series: str
    number: int
    total: int
    returncode: int
    failed: tuple[str, ...]

    @property
    def green(self) -> bool:
        """A run is green only when pytest exited 0 and named no failing test."""
        return self.returncode == 0 and not self.failed

    def describe(self) -> str:
        """``rack run 7/20: tests/test_rack_scale.py::TestX::test_y`` for a red run."""
        cases = ", ".join(self.failed)
        if not cases:
            cases = f"exit {self.returncode}, no case named"
        return f"{self.series} run {self.number}/{self.total}: {cases}"


@dataclass(frozen=True)
class NightReport:
    """The night's verdict: every run, the head it ran on, and where the pipeline lives."""

    runs: tuple[SoakRun, ...]
    sha: str
    pipeline_url: str
    date: str

    @property
    def green(self) -> bool:
        """The night is green only when every run was; one red run is a red night."""
        return all(run.green for run in self.runs)

    @property
    def red_runs(self) -> tuple[SoakRun, ...]:
        """The runs that went red, in the order they ran, for the report to name."""
        return tuple(run for run in self.runs if not run.green)

    def tally(self) -> str:
        """``soak 8/8, rack 19/20``: green runs over total, per series, in series order."""
        counts: dict[str, list[int]] = {}
        for run in self.runs:
            pair = counts.setdefault(run.series, [0, 0])
            pair[0] += int(run.green)
            pair[1] += 1
        return ", ".join(f"{series} {ok}/{total}" for series, (ok, total) in counts.items())

    def headline(self) -> str:
        """One line naming the verdict, the date, and the commit, for the issue and Discord."""
        verdict = "green" if self.green else "RED"
        return f"Nightly soak {verdict} {self.date} at {self.sha}: {self.tally()}"

    def issue_note(self, streak: int) -> str:
        """The note for #68; the trailing marker is what the streak count reads back.

        Args:
            streak: consecutive green nights including this one; ignored on a red night.
        """
        verdict = "green" if self.green else "red"
        marker = f"<!-- nightly-soak verdict={verdict} sha={self.sha} -->"
        if self.green:
            body = f"{self.headline()}. Green night {streak} of {GREEN_STREAK_TARGET} consecutive."
        else:
            failures = "\n".join(f"- {run.describe()}" for run in self.red_runs)
            body = f"{self.headline()}. Streak reset.\n\n{failures}"
        return f"{body}\n\nPipeline: {self.pipeline_url}\n\n{marker}"

    def discord_message(self) -> str:
        """The red-night post for #general, held under Discord's message limit."""
        failures = "\n".join(f"- {run.describe()}" for run in self.red_runs)
        text = f"{self.headline()}.\n{failures}\nPipeline: {self.pipeline_url}"
        if len(text) > DISCORD_MESSAGE_LIMIT:
            text = text[: DISCORD_MESSAGE_LIMIT - 1] + "…"
        return text


class NightlySoak:
    """One night's soak, from the first pytest run to the note on the issue.

    ``run`` returns the process exit code: 0 when every run was green, 1 otherwise.
    Missing credentials are printed and skipped; a posting call that fails raises, so
    the pipeline goes red with the cause in its log rather than a night going missing
    quietly.
    """

    def __init__(
        self,
        *,
        env: Mapping[str, str],
        post: bool,
        runner: Runner | None = None,
        http: Http | None = None,
        log_dir: Path = LOG_DIR,
        out: TextIO = sys.stdout,
    ) -> None:
        self._env = env
        self._post = post
        self._runner: Runner = runner if runner is not None else _run_pytest
        self._http: Http = http if http is not None else _http
        self._log_dir = log_dir
        self._out = out
        self._soak_runs = _count(env, "SOAK_RUNS", DEFAULT_SOAK_RUNS)
        self._rack_repeats = _count(env, "RACK_REPEATS", DEFAULT_RACK_REPEATS)

    def run(self) -> int:
        """Run both series, print the verdict, record it, and return the exit code."""
        self._log_dir.mkdir(parents=True, exist_ok=True)
        runs = [
            *self._series("soak", (), self._soak_runs),
            *self._series("rack", (RACK_SUITE,), self._rack_repeats),
        ]
        report = NightReport(
            runs=tuple(runs),
            sha=self._env.get("CI_COMMIT_SHORT_SHA", "local"),
            pipeline_url=self._env.get("CI_PIPELINE_URL", "(no pipeline)"),
            date=datetime.now(UTC).date().isoformat(),
        )
        self._say(report.headline())
        for run in report.red_runs:
            self._say(f"  {run.describe()}")
        if self._post:
            self._record(report)
        else:
            self._say("posting disabled; nothing recorded")
        return 0 if report.green else 1

    def _series(self, series: str, target: Sequence[str], total: int) -> list[SoakRun]:
        runs: list[SoakRun] = []
        for number in range(1, total + 1):
            self._say(f"{series} {number}/{total}")
            seed = secrets.randbits(64)
            argv = ["uv", "run", "pytest", f"--hypothesis-seed={seed}", "-q", "-rfE", *target]
            replay = f"$ {shlex.join(argv)}\n"
            log = self._log_dir / f"{series}-{number:02d}.log"
            log.write_text(replay)
            self._say(replay.rstrip())
            result = self._runner(argv)
            with log.open("a") as output:
                output.write(result.stdout + result.stderr)
            failed = tuple(dict.fromkeys(_FAILURE_LINE.findall(result.stdout)))
            runs.append(SoakRun(series, number, total, result.returncode, failed))
        return runs

    def _record(self, report: NightReport) -> None:
        token = self._env.get("LITMUS_BOT_TOKEN")
        if token:
            streak = 0
            if report.green:
                streak = self._streak(token) + 1
            note = {"body": report.issue_note(streak)}
            self._api(token, "POST", f"issues/{ISSUE_IID}/notes", note)
            self._say(f"recorded on #{ISSUE_IID}")
        else:
            self._say(f"LITMUS_BOT_TOKEN unset; night not recorded on #{ISSUE_IID}")
        if report.green:
            return
        webhook = self._env.get("DISCORD_WEBHOOK_URL")
        if not webhook:
            self._say("DISCORD_WEBHOOK_URL unset; red night not posted to #general")
            return
        headers = {"Content-Type": "application/json"}
        self._http("POST", webhook, headers, {"content": report.discord_message()})
        self._say("posted to #general")

    def _streak(self, token: str) -> int:
        """Consecutive green nights already on the issue, newest first, up to a red one.

        Notes without a marker (people talking on the issue) neither count nor break
        the streak.
        """
        path = f"issues/{ISSUE_IID}/notes?sort=desc&order_by=created_at&per_page=100"
        notes = cast("list[dict[str, Any]]", self._api(token, "GET", path))
        streak = 0
        for note in notes:
            match = _MARKER.search(str(note.get("body", "")))
            if match is None:
                continue
            if match.group(1) != "green":
                break
            streak += 1
        return streak

    def _api(self, token: str, method: str, path: str, body: object | None = None) -> object:
        url = f"{self._env['CI_API_V4_URL']}/projects/{self._env['CI_PROJECT_ID']}/{path}"
        headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
        return self._http(method, url, headers, body)

    def _say(self, line: str) -> None:
        self._out.write(line + "\n")
        self._out.flush()


def _count(env: Mapping[str, str], name: str, default: int) -> int:
    """A positive run count from the environment, or the default when unset or empty."""
    raw = env.get(name, "").strip()
    if not raw:
        return default
    count = int(raw)
    if count < 1:
        msg = f"{name} must be at least 1, got {count}"
        raise ValueError(msg)
    return count


def _run_pytest(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """One pytest run from the repo root; the exit code is the verdict, never an exception."""
    return subprocess.run(list(argv), cwd=ROOT, check=False, capture_output=True, text=True)


def _http(method: str, url: str, headers: Mapping[str, str], body: object | None) -> object:
    """One JSON request; the decoded response, or None when the server sent no body."""
    data = None if body is None else json.dumps(body).encode()
    req = request.Request(url, data=data, method=method, headers=dict(headers))
    with request.urlopen(req, timeout=60) as response:
        raw = response.read()
    if not raw:
        return None
    return json.loads(raw)


if __name__ == "__main__":
    raise SystemExit(NightlySoak(env=os.environ, post="--no-post" not in sys.argv[1:]).run())
