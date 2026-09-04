"""Deterministic main-versus-audit-versus-run-abort race coverage."""

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum

import pytest

from litmus import (
    PASS,
    AfterEachContext,
    BeforeEachContext,
    ExternalIntegrationFailure,
    LitmusTest,
    TestFailure,
    TestUtils,
    Verdict,
)
from litmus.lifecycle import State
from litmus.results import RunResult, SummaryTreeNode


class _RaceArm(StrEnum):
    MAIN = "main"
    AUDIT = "audit"
    RUN_ABORT = "run-abort"


type _RaceOrder = tuple[_RaceArm, _RaceArm, _RaceArm]


_RACE_ORDERS: tuple[_RaceOrder, ...] = (
    (_RaceArm.MAIN, _RaceArm.AUDIT, _RaceArm.RUN_ABORT),
    (_RaceArm.MAIN, _RaceArm.RUN_ABORT, _RaceArm.AUDIT),
    (_RaceArm.AUDIT, _RaceArm.MAIN, _RaceArm.RUN_ABORT),
    (_RaceArm.AUDIT, _RaceArm.RUN_ABORT, _RaceArm.MAIN),
    (_RaceArm.RUN_ABORT, _RaceArm.MAIN, _RaceArm.AUDIT),
    (_RaceArm.RUN_ABORT, _RaceArm.AUDIT, _RaceArm.MAIN),
)


@dataclass(slots=True)
class _RaceControls:
    main_gate: asyncio.Event = field(default_factory=asyncio.Event)
    audit_gate: asyncio.Event = field(default_factory=asyncio.Event)
    abort_gate: asyncio.Event = field(default_factory=asyncio.Event)
    main_started: asyncio.Event = field(default_factory=asyncio.Event)
    audit_started: asyncio.Event = field(default_factory=asyncio.Event)
    abort_started: asyncio.Event = field(default_factory=asyncio.Event)
    watched_settled: asyncio.Event = field(default_factory=asyncio.Event)
    observed: list[_RaceArm] = field(default_factory=list[_RaceArm])

    def release(self, arm: _RaceArm) -> None:
        """Release one contender without consulting elapsed time."""
        gates = {
            _RaceArm.MAIN: self.main_gate,
            _RaceArm.AUDIT: self.audit_gate,
            _RaceArm.RUN_ABORT: self.abort_gate,
        }
        gates[arm].set()


class _WatchedSuite(LitmusTest):
    """Expose independently gated authored-main and audit settlements."""

    def __init__(
        self,
        *,
        name: str,
        controls: _RaceControls,
        audit_failure: TestFailure,
    ) -> None:
        super().__init__(name=name)
        self._controls = controls
        self._audit_failure = audit_failure
        self.use_audit(interval=0.000_001, fn=self._audit)

    def build_suite(self) -> LitmusTest:
        """Return the single authored claim watched by the audit."""
        return self.case("authored main", self._main)

    async def _main(self, utils: TestUtils) -> Verdict:
        self._controls.main_started.set()
        await self._controls.main_gate.wait()
        self._controls.observed.append(_RaceArm.MAIN)
        if self._controls.main_gate.is_set():
            return PASS
        raise TestFailure("main gate resumed without release")

    async def _audit(self, utils: TestUtils) -> object:
        self._controls.audit_started.set()
        await self._controls.audit_gate.wait()
        self._controls.observed.append(_RaceArm.AUDIT)
        raise self._audit_failure


class _AbortProbe(LitmusTest):
    """Give the root integration hook a concurrent node on which to halt."""

    def build_suite(self) -> LitmusTest:
        """Return a claim that remains unreachable when its before hook aborts."""
        return self.case("unreachable claim", self._claim)

    async def _claim(self, utils: TestUtils) -> Verdict:
        if utils.logger.scope[-1] == "unreachable claim":
            return PASS
        raise TestFailure("abort probe received the wrong scope")


class _RaceRoot(LitmusTest):
    """Compose the watched node beside the independent abort trigger."""

    def __init__(self, *, name: str, watched: LitmusTest, abort_probe: LitmusTest) -> None:
        super().__init__(name=name)
        self._watched = watched
        self._abort_probe = abort_probe

    def build_suite(self) -> LitmusTest:
        """Run the three race contenders under one shared run-abort signal."""
        return self.group("race", [self._watched, self._abort_probe])


def _node_named(node: SummaryTreeNode, name: str) -> SummaryTreeNode:
    if node.name == name:
        return node
    for child in node.children:
        try:
            return _node_named(child, name)
        except LookupError:
            continue
    raise LookupError(name)


class TestThreeWayRace:
    @pytest.mark.parametrize(
        "order",
        _RACE_ORDERS,
        ids=(
            "main-audit-run-abort",
            "main-run-abort-audit",
            "audit-main-run-abort",
            "audit-run-abort-main",
            "run-abort-main-audit",
            "run-abort-audit-main",
        ),
    )
    async def test_first_terminal_signal_owns_each_strict_ordering(
        self,
        order: _RaceOrder,
    ) -> None:
        controls = _RaceControls()
        audit_failure = TestFailure("watchdog found an unsafe temperature")
        abort_cause = RuntimeError("shop-floor coordinator unavailable")
        watched_results: list[RunResult] = []

        async def before_each(ctx: BeforeEachContext) -> None:
            if ctx.name != "abort trigger":
                return
            controls.abort_started.set()
            await controls.abort_gate.wait()
            controls.observed.append(_RaceArm.RUN_ABORT)
            raise abort_cause

        async def after_each(ctx: AfterEachContext) -> None:
            if ctx.name == "watched":
                watched_results.append(ctx.result)
                controls.watched_settled.set()

        watched = _WatchedSuite(
            name="watched",
            controls=controls,
            audit_failure=audit_failure,
        )
        root = _RaceRoot(
            name="rack",
            watched=watched,
            abort_probe=_AbortProbe(name="abort trigger"),
        )
        running = asyncio.create_task(root.run(before_each=before_each, after_each=after_each))

        await asyncio.gather(
            controls.main_started.wait(),
            controls.audit_started.wait(),
            controls.abort_started.wait(),
        )

        result: RunResult | None = None
        for index, arm in enumerate(order):
            controls.release(arm)
            if arm is _RaceArm.RUN_ABORT:
                result = await running
                for cancelled_arm in order[index + 1 :]:
                    controls.release(cancelled_arm)
                break
            if index == 0:
                await controls.watched_settled.wait()

        assert result is not None
        assert result.state is State.ABORTED
        assert isinstance(result.failure, ExternalIntegrationFailure)
        assert result.failure.cause is abort_cause
        assert len(watched_results) == 1

        watched_result = watched_results[0]
        expected_state = {
            _RaceArm.MAIN: State.PASS,
            _RaceArm.AUDIT: State.FAIL,
            _RaceArm.RUN_ABORT: State.ABORTED,
        }[order[0]]
        assert watched_result.state is expected_state
        assert _node_named(result.summary_tree, "watched").state is expected_state

        if expected_state is State.PASS:
            assert watched_result.failure is None
            assert controls.observed == [_RaceArm.MAIN, _RaceArm.RUN_ABORT]
        elif expected_state is State.FAIL:
            assert watched_result.failure is audit_failure
            assert controls.observed == [_RaceArm.AUDIT, _RaceArm.RUN_ABORT]
        else:
            assert isinstance(watched_result.failure, ExternalIntegrationFailure)
            assert watched_result.failure.cause is abort_cause
            assert controls.observed == [_RaceArm.RUN_ABORT]
