"""The Re-Run session: where the Inspector executes suite code, and what it leaves behind."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest
from tests.inspector_rack import (
    MIXED_ROWS,
    PARK,
    mixed_suite,
    other_suite,
    parked_suite,
    record_mixed_run,
    record_parked_run,
)

from litmus import PASS, LitmusTest, TestUtils, Verdict
from litmus.base import declared_tree
from litmus.inspector_rerun import RerunSession, first_mismatch, subtree_indices, subtree_of
from litmus.inspector_timeline import TimelineRecord
from litmus.run_data import JsonObject

POWER, CHECKS, VOLTS, SLOW, LATER = 2, 3, 4, 5, 6
"""Row indices in ``MIXED_ROWS``; the recording pins them."""


def _state(session: RerunSession, index: int) -> str | None:
    """The re-run state of a row, or None when no re-run reached it."""
    record = session.record(index)
    return None if record is None else record.row.state


def _source(session: RerunSession, index: int) -> int | None:
    """Which pressed row produced a row's record, or None when no re-run reached it."""
    record = session.record(index)
    return None if record is None else record.source


def _refusing() -> LitmusTest:
    raise RuntimeError("no such module: bench")


async def _mixed_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RerunSession:
    saved = await record_mixed_run(tmp_path, monkeypatch, refuse_execution=False)
    session = RerunSession(saved, mixed_suite, scratch_parent=tmp_path)
    await session.prepare()
    return session


class TestPreparing:
    async def test_a_matching_suite_is_available_and_nothing_has_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = await _mixed_session(tmp_path, monkeypatch)

        assert session.available is True
        assert session.unavailable is None
        assert session.scratch_root is None
        assert all(session.record(index) is None for index in range(len(MIXED_ROWS)))
        assert await session.select(POWER) is None

    async def test_a_suite_of_another_shape_is_refused_at_the_first_differing_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_mixed_run(tmp_path, monkeypatch, refuse_execution=False)
        session = RerunSession(saved, other_suite, scratch_parent=tmp_path)

        await session.prepare()

        assert session.available is False
        assert session.unavailable is not None
        assert session.unavailable.startswith(
            "the supplied suite differs from the saved run at rack/steps: "
            "saved 'steps' (sequence), suite 'devices' (parallel)"
        )
        assert "--suite" in session.unavailable
        # A refused suite never runs: Enter is inert and no scratch directory appears.
        await session.run(POWER)
        assert session.scratch_root is None

    async def test_a_suite_that_cannot_load_names_the_loader_s_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_mixed_run(tmp_path, monkeypatch)
        session = RerunSession(saved, _refusing, scratch_parent=tmp_path)

        await session.prepare()

        assert session.available is False
        assert session.unavailable == "RuntimeError: no such module: bench"

    async def test_no_suite_keeps_the_reason_the_command_line_gave(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_mixed_run(tmp_path, monkeypatch)
        session = RerunSession(saved, "reopen with --suite", scratch_parent=tmp_path)

        await session.prepare()

        assert session.available is False
        assert session.unavailable == "reopen with --suite"

    async def test_the_mismatch_report_counts_a_shorter_suite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_mixed_run(tmp_path, monkeypatch)

        class _Shorter(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.sequence("steps", [self.test("power", self._power)])

            async def _power(self, utils: TestUtils) -> Verdict:
                return PASS

        message = first_mismatch(saved.tree.rows, declared_tree(_Shorter(name="rack")))
        assert message is not None
        assert message.startswith("the supplied suite declares 3 nodes where the saved run has 7")


class TestRunning:
    async def test_a_leaf_re_run_records_the_leaf_and_its_ancestors_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = await _mixed_session(tmp_path, monkeypatch)

        await session.run(POWER)

        assert session.running is None
        assert session.diagnostic is None
        states = {
            index: None if (record := session.record(index)) is None else record.row.state
            for index in range(len(MIXED_ROWS))
        }
        # The path to the leaf ran its lifecycle; every other leaf was left pending
        # and so has no record, exactly as the sealed run left them.
        assert states == {0: "Pass", 1: "Pass", 2: "Pass", 3: None, 4: None, 5: None, 6: None}
        assert session.scratch_root is not None
        assert session.scratch_root.name.startswith("litmus-rerun-")
        assert len(list((session.scratch_root / str(POWER)).rglob("result.json"))) == 1

        view = await session.select(POWER)
        assert view is not None
        assert view.status == "ready"
        assert view.owner is not None
        assert view.owner.ancestor_names == ("rack", "steps")
        assert view.timeline is not None
        assert any(
            isinstance(row, TimelineRecord)
            and "ripple above expectation" in str(row.data.get("message", ""))
            for row in view.timeline.rows
        )
        assert await session.select(VOLTS) is None

    async def test_a_composite_re_run_executes_every_claim_beneath_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = await _mixed_session(tmp_path, monkeypatch)

        await session.run(CHECKS)

        assert _state(session, VOLTS) == "Fail"
        assert _state(session, SLOW) == "Aborted"
        assert _state(session, POWER) is None
        assert _state(session, LATER) is None
        assert _state(session, CHECKS) == "Aborted"
        assert _source(session, CHECKS) == CHECKS

    async def test_re_running_a_node_replaces_its_previous_evidence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = await _mixed_session(tmp_path, monkeypatch)
        await session.run(POWER)
        assert session.scratch_root is not None
        first = next((session.scratch_root / str(POWER)).rglob("result.json"))
        first_bytes = first.read_bytes()

        await session.run(POWER)

        results = list((session.scratch_root / str(POWER)).rglob("result.json"))
        assert len(results) == 1
        assert not first.exists() or results[0].read_bytes() != first_bytes

    async def test_a_later_re_run_of_an_ancestor_supersedes_a_leaf_s_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = await _mixed_session(tmp_path, monkeypatch)
        await session.run(VOLTS)
        assert _source(session, VOLTS) == VOLTS

        await session.run(CHECKS)

        assert _source(session, VOLTS) == CHECKS
        # Re-running the leaf again drops the composite's record of it, but the
        # composite's own record of its other child stays, because that evidence is
        # still on disk under the composite's directory.
        await session.run(VOLTS)
        assert _source(session, VOLTS) == VOLTS
        assert _source(session, SLOW) == CHECKS

    async def test_close_removes_everything_the_bench_wrote(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = await _mixed_session(tmp_path, monkeypatch)
        await session.run(POWER)
        root = session.scratch_root
        assert root is not None
        assert root.exists()

        session.close()

        assert not root.exists()
        assert session.scratch_root is None
        assert session.record(POWER) is None
        assert session.view is None
        session.close()

    async def test_a_lifecycle_that_cannot_start_is_reported_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = await _mixed_session(tmp_path, monkeypatch)

        class _Sabotage(LitmusTest):
            def build_suite(self) -> LitmusTest:
                raise RuntimeError("build_suite exploded")

        monkeypatch.setattr(session, "_factory", lambda: _Sabotage(name="rack"))

        await session.run(POWER)

        assert session.running is None
        assert session.diagnostic is not None
        assert session.diagnostic.startswith("re-run of rack/steps/power did not settle: ")
        assert "build_suite exploded" in session.diagnostic
        assert session.record(POWER) is None


class TestInterrupting:
    async def test_interrupt_takes_the_operator_path_and_records_aborted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_parked_run(tmp_path)
        session = RerunSession(saved, parked_suite, scratch_parent=tmp_path)
        await session.prepare()
        started = asyncio.Event()
        monkeypatch.setitem(PARK, "event", started)

        running = asyncio.create_task(session.run(1))
        await asyncio.wait_for(started.wait(), 5)
        assert session.running == 1
        assert session.running_indices() == range(1, 2)

        session.interrupt()
        await asyncio.wait_for(running, 10)

        assert session.running is None
        record = session.record(1)
        assert record is not None
        assert record.row.state == "Aborted"
        failure = cast(JsonObject, record.row.record["failure"])
        assert failure["name"] == "OperatorInterrupt"
        # Nothing is running now, so a second interrupt is a no-op rather than a signal.
        session.interrupt()


class TestPositions:
    async def test_subtree_positions_and_descendant_ranges_follow_declaration_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saved = await record_mixed_run(tmp_path, monkeypatch)
        rows = saved.tree.rows

        assert subtree_of(rows, 0) == ()
        assert subtree_of(rows, 1) == (0,)
        assert subtree_of(rows, POWER) == (0, 0)
        assert subtree_of(rows, SLOW) == (0, 1, 1)
        assert subtree_of(rows, LATER) == (0, 2)
        assert subtree_indices(rows, 0) == range(0, 7)
        assert subtree_indices(rows, CHECKS) == range(3, 6)
        assert subtree_indices(rows, LATER) == range(6, 7)
