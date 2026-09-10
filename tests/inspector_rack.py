"""Record one 70-DUT rack run and reopen it from saved bytes alone, for Inspector tests.

The Inspector's acceptance is a saved 70-DUT rack inspectable without live ``LitmusTest``
objects, source discovery, or the station that produced it (``docs/inspector.md``).
Every Inspector test module proves that against the same shape of run; this helper is
the one place that shape is written, so a screen test and a model test open the same
evidence. Each device captures two ``survey`` artifacts, one carrying ``None`` and one
carrying the device name, so a comparison of the pair has a real difference to show.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from litmus import PASS, Artifact, LitmusTest, TestUtils, Verdict
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
