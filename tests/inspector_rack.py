"""Record one 70-DUT rack run and reopen it from saved bytes alone, for Inspector tests.

The Inspector's acceptance is a saved 70-DUT rack inspectable without live ``LitmusTest``
objects, source discovery, or the station that produced it (``docs/inspector.md``).
Every Inspector test module proves that against the same shape of run; this helper is
the one place that shape is written, so a screen test and a model test open the same
evidence. Each device captures two ``survey`` artifacts, one carrying ``None`` and one
carrying the device name, so a comparison of the pair has a real difference to show.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from litmus import PASS, Artifact, LitmusTest, TestFailure, TestUtils, Verdict
from litmus.inspector_run import SavedRun, SavedRunReader

DEVICE_COUNT = 70


def device_row(index: int) -> int:
    """The tree row of device ``index``'s claim.

    Rows are declaration preorder: root, the ``devices`` parallel, then each device
    followed by its one ``voltage`` claim, so the claim of device ``i`` sits at
    ``3 + 2 * i``.
    """
    return 3 + 2 * index


class _Device(LitmusTest):
    def build_suite(self) -> LitmusTest:
        return self.test("voltage", self._voltage)

    async def _voltage(self, utils: TestUtils) -> Verdict:
        for value in [None, self.name]:
            await utils.capture_artifact(Artifact(name="reading", type="survey", data=value))
        if self.name:
            return PASS
        raise AssertionError("device name required")


class _Rack(LitmusTest):
    def build_suite(self) -> LitmusTest:
        return self.parallel("devices", [_Device(name=f"dut-{i:02d}") for i in range(DEVICE_COUNT)])


async def record_seventy_dut_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SavedRun:
    """Run the rack once, move its directory, forbid execution, and reopen the evidence.

    The move is what makes the test honest: every reference inside the run was written
    for the original location, and the reader must follow the recorded relocation
    evidence rather than the path that no longer exists. ``LitmusTest.run`` is replaced
    so any attempt to reconstruct the suite fails loudly.
    """
    rack = _Rack(name="rack")
    await rack.run()
    assert rack.log_path is not None
    moved = tmp_path / "moved"
    rack.log_path.parent.parent.rename(moved)
    del rack

    def refuse_execution(*args: object, **kwargs: object) -> None:
        raise AssertionError("an Inspector test attempted suite execution")

    monkeypatch.setattr(LitmusTest, "run", refuse_execution)
    saved = await SavedRunReader().read(moved / "result.json")
    assert isinstance(saved, SavedRun), saved
    return saved


def saved_bytes(run_root: Path) -> dict[Path, bytes]:
    """Every file under the run, for asserting that inspection changed nothing."""
    return {path: path.read_bytes() for path in run_root.rglob("*") if path.is_file()}


class _Mixed(LitmusTest):
    """A small run with every recorded state, for failure navigation and the log pane.

    The sequence stops at the parallel's failure, so ``later`` is pending. Inside the
    parallel one claim fails and one is aborted by its time budget, so both kinds of
    attention are reachable. ``power`` passes after logging an error and capturing an
    artifact, so the log pane has a capture number and an errors-only row to show.
    """

    def build_suite(self) -> LitmusTest:
        return self.sequence(
            "steps",
            [
                self.test("power", self._power),
                self.parallel(
                    "checks",
                    [
                        self.test("volts", self._volts),
                        self.test("slow", self._slow, abort_after=0.05),
                    ],
                ),
                self.test("later", self._later),
            ],
        )

    async def _power(self, utils: TestUtils) -> Verdict:
        await utils.capture_artifact(Artifact(name="rail", type="survey", data={"v": 12.1}))
        utils.logger.info("rail settled")
        utils.logger.error("ripple above expectation")
        if utils.exec_route == "local":
            return PASS
        raise AssertionError("the mixed run never leaves the test host")

    async def _volts(self, utils: TestUtils) -> Verdict:
        raise TestFailure("rail outside tolerance", "12.0 V", "12.1 V")

    async def _slow(self, utils: TestUtils) -> Verdict:
        await asyncio.sleep(10)
        return PASS

    async def _later(self, utils: TestUtils) -> Verdict:
        return PASS


MIXED_ROWS = ("rack", "steps", "power", "checks", "volts", "slow", "later")
"""The mixed run's preorder row names; ``volts`` is Fail, ``slow`` Aborted, ``later`` pending."""


async def record_mixed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, refuse_execution: bool = True
) -> SavedRun:
    """Record the mixed-state run, move it, forbid execution, and reopen it.

    A Re-Run test passes ``refuse_execution=False``: that tab is the one place the
    Inspector executes suite code by ruling, and its tests prove what it runs rather
    than that it runs nothing.
    """
    rack = _Mixed(name="rack")
    await rack.run()
    assert rack.log_path is not None
    moved = tmp_path / "mixed"
    rack.log_path.parent.parent.rename(moved)
    del rack

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("an Inspector test attempted suite execution")

    if refuse_execution:
        monkeypatch.setattr(LitmusTest, "run", refuse)
    saved = await SavedRunReader().read(moved / "result.json")
    assert isinstance(saved, SavedRun), saved
    return saved


def mixed_suite() -> LitmusTest:
    """A fresh instance of the mixed suite, the shape ``record_mixed_run`` saved."""
    return _Mixed(name="rack")


def other_suite() -> LitmusTest:
    """A fresh rack suite: a different declared tree, for the Re-Run refusal."""
    return _Rack(name="rack")


PARK: dict[str, asyncio.Event | None] = {"event": None}
"""Set ``PARK["event"]`` to make the parked suite wait; it passes at once otherwise."""


class _Parked(LitmusTest):
    """Passes at once when recording; on re-run, parks until the operator interrupts."""

    def build_suite(self) -> LitmusTest:
        return self.test("wait", self._wait)

    async def _wait(self, utils: TestUtils) -> Verdict:
        event = PARK["event"]
        if event is None:
            return PASS
        event.set()
        await asyncio.sleep(3600)
        return PASS


def parked_suite() -> LitmusTest:
    """A fresh parked suite, the shape ``record_parked_run`` saved."""
    return _Parked(name="bench")


async def record_parked_run(tmp_path: Path) -> SavedRun:
    """Record the parked suite passing, move it, and reopen it for a live re-run test."""
    bench = _Parked(name="bench")
    await bench.run()
    assert bench.log_path is not None
    moved = tmp_path / "parked"
    bench.log_path.parent.parent.rename(moved)
    saved = await SavedRunReader().read(moved / "result.json")
    assert isinstance(saved, SavedRun), saved
    return saved
