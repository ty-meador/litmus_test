"""The child-boundary wrapper — every cell of the classification matrix.

Eleven ways a call into suite code can end, times seven lifecycle slots it can end in.
The table below is the specification: each cell states what the boundary must make of
that combination, and the parametrized test walks all seventy-seven. Nothing here
recomputes the policy — if the table and the wrapper ever disagree, the table wins.
"""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, final

import pytest

from litmus import (
    PASS,
    ContractViolation,
    ExternalIntegrationFailure,
    FrameworkContractViolation,
    MissingVerdict,
    TestFailure,
)

# Internal machinery on purpose: the boundary is the subject under test here, and the
# self-test suite is the one consumer allowed to reach past the public surface.
from litmus.lifecycle import SLOT_POLICIES, ChildBoundary, Outcome, RunAbort, Slot, State

NODE = "dimm-0"
WRONG_VALUE = 42
HOOK_CAUSE = RuntimeError("shop-floor database refused the record")


class DimmFailure(TestFailure):
    """A suite-authored `TestFailure` subclass, as a real DIMM test would define."""


class Cause(StrEnum):
    """The eleven ways a call into suite code can end."""

    PASS_SENTINEL = "PASS sentinel"
    PASS_STRING = "'PASS' string"
    NONE_RETURN = "None"
    WRONG_VALUE = "wrong value"
    TEST_FAILURE = "TestFailure"
    TEST_FAILURE_SUBCLASS = "TestFailure subclass"
    PLAIN_EXCEPTION = "plain Exception"
    BASE_EXCEPTION = "BaseException"
    CANCELLED_FROM_ABORT = "CancelledError from abort"
    CANCELLED_SPURIOUS = "CancelledError spurious"
    INTEGRATION_FAILURE = "ExternalIntegrationFailure"


@final
class Subject:
    """Suite code that misbehaves in exactly one way, on demand.

    Records what it raised so the tests can assert on identity — an
    `ExternalIntegrationFailure` must cross the boundary as the same object, not an
    equal one.
    """

    def __init__(self, run_abort: RunAbort) -> None:
        self._run_abort = run_abort
        self.raised: BaseException | None = None
        self._calls: dict[Cause, Callable[[], Coroutine[Any, Any, object]]] = {
            Cause.PASS_SENTINEL: self._returns_pass_sentinel,
            Cause.PASS_STRING: self._returns_pass_string,
            Cause.NONE_RETURN: self._returns_none,
            Cause.WRONG_VALUE: self._returns_wrong_value,
            Cause.TEST_FAILURE: self._raises_test_failure,
            Cause.TEST_FAILURE_SUBCLASS: self._raises_test_failure_subclass,
            Cause.PLAIN_EXCEPTION: self._raises_plain_exception,
            Cause.BASE_EXCEPTION: self._raises_base_exception,
            Cause.CANCELLED_FROM_ABORT: self._raises_cancelled_from_abort,
            Cause.CANCELLED_SPURIOUS: self._raises_cancelled_spurious,
            Cause.INTEGRATION_FAILURE: self._raises_integration_failure,
        }

    def call(self, cause: Cause) -> Coroutine[Any, Any, object]:
        """The awaitable the boundary will wrap."""
        return self._calls[cause]()

    def _remember[E: BaseException](self, error: E) -> E:
        self.raised = error
        return error

    async def _returns_pass_sentinel(self) -> object:
        return PASS

    async def _returns_pass_string(self) -> object:
        return "PASS"

    async def _returns_none(self) -> object:
        return None

    async def _returns_wrong_value(self) -> object:
        return WRONG_VALUE

    async def _raises_test_failure(self) -> object:
        raise self._remember(TestFailure("firmware mismatch", expected="2.4.1", received="2.3.9"))

    async def _raises_test_failure_subclass(self) -> object:
        raise self._remember(DimmFailure("rank 1 unreadable"))

    async def _raises_plain_exception(self) -> object:
        raise self._remember(ValueError("kaboom"))

    async def _raises_base_exception(self) -> object:
        raise self._remember(KeyboardInterrupt())

    async def _raises_cancelled_from_abort(self) -> object:
        self._run_abort.fire(ExternalIntegrationFailure("before_each", HOOK_CAUSE))
        raise self._remember(asyncio.CancelledError())

    async def _raises_cancelled_spurious(self) -> object:
        raise self._remember(asyncio.CancelledError())

    async def _raises_integration_failure(self) -> object:
        raise self._remember(ExternalIntegrationFailure("before_each", HOOK_CAUSE))


@final
@dataclass(frozen=True, slots=True)
class Expected:
    """What the boundary must make of one cell."""

    state: State | None
    raises: type[BaseException] | None = None
    failure: type[BaseException] | None = None
    swallowed: type[BaseException] | None = None
    records: bool = True
    """False where the boundary must not classify at all — the halt is outside the
    taxonomy and passes through untouched."""


_NON_VERDICT_SLOTS = (
    Slot.CONFIGURE,
    Slot.CLEANUP,
    Slot.SURVEY,
    Slot.AUDIT,
    Slot.BEFORE_EACH,
    Slot.AFTER_EACH,
)

# --- The matrix. Eleven causes by seven slots. -------------------------------------
MATRIX: dict[tuple[Cause, Slot], Expected] = {
    # The sentinel. Only a test method is asked for a verdict; elsewhere it is just a
    # return value, and the boundary has no opinion about it.
    (Cause.PASS_SENTINEL, Slot.TEST_METHOD): Expected(State.PASS),
    (Cause.PASS_SENTINEL, Slot.CONFIGURE): Expected(None),
    (Cause.PASS_SENTINEL, Slot.CLEANUP): Expected(None),
    (Cause.PASS_SENTINEL, Slot.SURVEY): Expected(None),
    (Cause.PASS_SENTINEL, Slot.AUDIT): Expected(None),
    (Cause.PASS_SENTINEL, Slot.BEFORE_EACH): Expected(None),
    (Cause.PASS_SENTINEL, Slot.AFTER_EACH): Expected(None),
    # The forged sentinel. Identity, not equality — the string must NOT pass.
    (Cause.PASS_STRING, Slot.TEST_METHOD): Expected(State.ABORTED, failure=MissingVerdict),
    (Cause.PASS_STRING, Slot.CONFIGURE): Expected(None),
    (Cause.PASS_STRING, Slot.CLEANUP): Expected(None),
    (Cause.PASS_STRING, Slot.SURVEY): Expected(None),
    (Cause.PASS_STRING, Slot.AUDIT): Expected(None),
    (Cause.PASS_STRING, Slot.BEFORE_EACH): Expected(None),
    (Cause.PASS_STRING, Slot.AFTER_EACH): Expected(None),
    # The forgotten return. Aborted, never Fail: no verdict exists to report.
    (Cause.NONE_RETURN, Slot.TEST_METHOD): Expected(State.ABORTED, failure=MissingVerdict),
    (Cause.NONE_RETURN, Slot.CONFIGURE): Expected(None),
    (Cause.NONE_RETURN, Slot.CLEANUP): Expected(None),
    (Cause.NONE_RETURN, Slot.SURVEY): Expected(None),
    (Cause.NONE_RETURN, Slot.AUDIT): Expected(None),
    (Cause.NONE_RETURN, Slot.BEFORE_EACH): Expected(None),
    (Cause.NONE_RETURN, Slot.AFTER_EACH): Expected(None),
    # Truthy junk. Same bucket as None — the split has exactly two branches.
    (Cause.WRONG_VALUE, Slot.TEST_METHOD): Expected(State.ABORTED, failure=MissingVerdict),
    (Cause.WRONG_VALUE, Slot.CONFIGURE): Expected(None),
    (Cause.WRONG_VALUE, Slot.CLEANUP): Expected(None),
    (Cause.WRONG_VALUE, Slot.SURVEY): Expected(None),
    (Cause.WRONG_VALUE, Slot.AUDIT): Expected(None),
    (Cause.WRONG_VALUE, Slot.BEFORE_EACH): Expected(None),
    (Cause.WRONG_VALUE, Slot.AFTER_EACH): Expected(None),
    # A real verdict of Fail. Caught and recorded — never control flow.
    (Cause.TEST_FAILURE, Slot.TEST_METHOD): Expected(State.FAIL, failure=TestFailure),
    (Cause.TEST_FAILURE, Slot.CONFIGURE): Expected(State.FAIL, failure=TestFailure),
    (Cause.TEST_FAILURE, Slot.CLEANUP): Expected(None, swallowed=TestFailure),
    (Cause.TEST_FAILURE, Slot.SURVEY): Expected(None, swallowed=TestFailure),
    (Cause.TEST_FAILURE, Slot.AUDIT): Expected(State.FAIL, failure=TestFailure),
    (Cause.TEST_FAILURE, Slot.BEFORE_EACH): Expected(
        State.ABORTED, raises=ExternalIntegrationFailure, failure=ExternalIntegrationFailure
    ),
    (Cause.TEST_FAILURE, Slot.AFTER_EACH): Expected(
        State.ABORTED, raises=ExternalIntegrationFailure, failure=ExternalIntegrationFailure
    ),
    # A subclass must reach the artifact class-intact, not flattened to its base.
    (Cause.TEST_FAILURE_SUBCLASS, Slot.TEST_METHOD): Expected(State.FAIL, failure=DimmFailure),
    (Cause.TEST_FAILURE_SUBCLASS, Slot.CONFIGURE): Expected(State.FAIL, failure=DimmFailure),
    (Cause.TEST_FAILURE_SUBCLASS, Slot.CLEANUP): Expected(None, swallowed=DimmFailure),
    (Cause.TEST_FAILURE_SUBCLASS, Slot.SURVEY): Expected(None, swallowed=DimmFailure),
    (Cause.TEST_FAILURE_SUBCLASS, Slot.AUDIT): Expected(State.FAIL, failure=DimmFailure),
    (Cause.TEST_FAILURE_SUBCLASS, Slot.BEFORE_EACH): Expected(
        State.ABORTED, raises=ExternalIntegrationFailure, failure=ExternalIntegrationFailure
    ),
    (Cause.TEST_FAILURE_SUBCLASS, Slot.AFTER_EACH): Expected(
        State.ABORTED, raises=ExternalIntegrationFailure, failure=ExternalIntegrationFailure
    ),
    # A developer bug. Lands in Fail, banners itself as a bug.
    (Cause.PLAIN_EXCEPTION, Slot.TEST_METHOD): Expected(State.FAIL, failure=ContractViolation),
    (Cause.PLAIN_EXCEPTION, Slot.CONFIGURE): Expected(State.FAIL, failure=ContractViolation),
    (Cause.PLAIN_EXCEPTION, Slot.CLEANUP): Expected(None, swallowed=ContractViolation),
    (Cause.PLAIN_EXCEPTION, Slot.SURVEY): Expected(None, swallowed=ContractViolation),
    (Cause.PLAIN_EXCEPTION, Slot.AUDIT): Expected(State.FAIL, failure=ContractViolation),
    (Cause.PLAIN_EXCEPTION, Slot.BEFORE_EACH): Expected(
        State.ABORTED, raises=ExternalIntegrationFailure, failure=ExternalIntegrationFailure
    ),
    (Cause.PLAIN_EXCEPTION, Slot.AFTER_EACH): Expected(
        State.ABORTED, raises=ExternalIntegrationFailure, failure=ExternalIntegrationFailure
    ),
    # A halt outside the taxonomy — Ctrl-C, SystemExit. The boundary has no business
    # classifying it and no business swallowing it, in any slot.
    (Cause.BASE_EXCEPTION, Slot.TEST_METHOD): Expected(
        None, raises=KeyboardInterrupt, records=False
    ),
    (Cause.BASE_EXCEPTION, Slot.CONFIGURE): Expected(None, raises=KeyboardInterrupt, records=False),
    (Cause.BASE_EXCEPTION, Slot.CLEANUP): Expected(None, raises=KeyboardInterrupt, records=False),
    (Cause.BASE_EXCEPTION, Slot.SURVEY): Expected(None, raises=KeyboardInterrupt, records=False),
    (Cause.BASE_EXCEPTION, Slot.AUDIT): Expected(None, raises=KeyboardInterrupt, records=False),
    (Cause.BASE_EXCEPTION, Slot.BEFORE_EACH): Expected(
        None, raises=KeyboardInterrupt, records=False
    ),
    (Cause.BASE_EXCEPTION, Slot.AFTER_EACH): Expected(
        None, raises=KeyboardInterrupt, records=False
    ),
    # The run is being torn down. Aborted, recorded with the reason, and the
    # cancellation continues outward — structured concurrency depends on it.
    (Cause.CANCELLED_FROM_ABORT, Slot.TEST_METHOD): Expected(
        State.ABORTED, raises=asyncio.CancelledError, failure=ExternalIntegrationFailure
    ),
    (Cause.CANCELLED_FROM_ABORT, Slot.CONFIGURE): Expected(
        State.ABORTED, raises=asyncio.CancelledError, failure=ExternalIntegrationFailure
    ),
    (Cause.CANCELLED_FROM_ABORT, Slot.CLEANUP): Expected(
        State.ABORTED, raises=asyncio.CancelledError, failure=ExternalIntegrationFailure
    ),
    (Cause.CANCELLED_FROM_ABORT, Slot.SURVEY): Expected(
        State.ABORTED, raises=asyncio.CancelledError, failure=ExternalIntegrationFailure
    ),
    (Cause.CANCELLED_FROM_ABORT, Slot.AUDIT): Expected(
        State.ABORTED, raises=asyncio.CancelledError, failure=ExternalIntegrationFailure
    ),
    (Cause.CANCELLED_FROM_ABORT, Slot.BEFORE_EACH): Expected(
        State.ABORTED, raises=asyncio.CancelledError, failure=ExternalIntegrationFailure
    ),
    (Cause.CANCELLED_FROM_ABORT, Slot.AFTER_EACH): Expected(
        State.ABORTED, raises=asyncio.CancelledError, failure=ExternalIntegrationFailure
    ),
    # Cancelled with nothing having asked for it. Still Aborted — no verdict was
    # produced — but the framework layer of the bug guard fires.
    (Cause.CANCELLED_SPURIOUS, Slot.TEST_METHOD): Expected(
        State.ABORTED, raises=asyncio.CancelledError, failure=FrameworkContractViolation
    ),
    (Cause.CANCELLED_SPURIOUS, Slot.CONFIGURE): Expected(
        State.ABORTED, raises=asyncio.CancelledError, failure=FrameworkContractViolation
    ),
    (Cause.CANCELLED_SPURIOUS, Slot.CLEANUP): Expected(
        State.ABORTED, raises=asyncio.CancelledError, failure=FrameworkContractViolation
    ),
    (Cause.CANCELLED_SPURIOUS, Slot.SURVEY): Expected(
        State.ABORTED, raises=asyncio.CancelledError, failure=FrameworkContractViolation
    ),
    (Cause.CANCELLED_SPURIOUS, Slot.AUDIT): Expected(
        State.ABORTED, raises=asyncio.CancelledError, failure=FrameworkContractViolation
    ),
    (Cause.CANCELLED_SPURIOUS, Slot.BEFORE_EACH): Expected(
        State.ABORTED, raises=asyncio.CancelledError, failure=FrameworkContractViolation
    ),
    (Cause.CANCELLED_SPURIOUS, Slot.AFTER_EACH): Expected(
        State.ABORTED, raises=asyncio.CancelledError, failure=FrameworkContractViolation
    ),
    # The halt signal itself, arriving from below. Propagates through every slot —
    # including the two that swallow ordinary errors — and is never rewrapped.
    (Cause.INTEGRATION_FAILURE, Slot.TEST_METHOD): Expected(
        State.ABORTED, raises=ExternalIntegrationFailure, failure=ExternalIntegrationFailure
    ),
    (Cause.INTEGRATION_FAILURE, Slot.CONFIGURE): Expected(
        State.ABORTED, raises=ExternalIntegrationFailure, failure=ExternalIntegrationFailure
    ),
    (Cause.INTEGRATION_FAILURE, Slot.CLEANUP): Expected(
        State.ABORTED, raises=ExternalIntegrationFailure, failure=ExternalIntegrationFailure
    ),
    (Cause.INTEGRATION_FAILURE, Slot.SURVEY): Expected(
        State.ABORTED, raises=ExternalIntegrationFailure, failure=ExternalIntegrationFailure
    ),
    (Cause.INTEGRATION_FAILURE, Slot.AUDIT): Expected(
        State.ABORTED, raises=ExternalIntegrationFailure, failure=ExternalIntegrationFailure
    ),
    (Cause.INTEGRATION_FAILURE, Slot.BEFORE_EACH): Expected(
        State.ABORTED, raises=ExternalIntegrationFailure, failure=ExternalIntegrationFailure
    ),
    (Cause.INTEGRATION_FAILURE, Slot.AFTER_EACH): Expected(
        State.ABORTED, raises=ExternalIntegrationFailure, failure=ExternalIntegrationFailure
    ),
}

_CELLS = [
    pytest.param(cause, slot, expected, id=f"{cause.value} in {slot.value}")
    for (cause, slot), expected in MATRIX.items()
]


class TestMatrixCoverage:
    """The matrix is only an exit criterion if it is provably complete."""

    def test_every_cause_meets_every_slot(self) -> None:
        missing = {(cause, slot) for cause in Cause for slot in Slot} - set(MATRIX)
        assert missing == set()

    def test_the_matrix_states_nothing_that_does_not_exist(self) -> None:
        assert len(MATRIX) == len(Cause) * len(Slot) == 77

    def test_every_slot_declares_a_policy(self) -> None:
        assert set(SLOT_POLICIES) == set(Slot)

    def test_only_test_methods_are_asked_for_a_verdict(self) -> None:
        demanding = {slot for slot, policy in SLOT_POLICIES.items() if policy.demands_verdict}
        assert demanding == {Slot.TEST_METHOD}
        assert set(_NON_VERDICT_SLOTS) == set(Slot) - demanding


class TestClassificationMatrix:
    async def _classify(self, cause: Cause, slot: Slot, expected: Expected) -> Outcome | None:
        run_abort = RunAbort()
        boundary = ChildBoundary(NODE, run_abort)
        subject = Subject(run_abort)
        if expected.raises is None:
            return await boundary.run(slot, subject.call(cause))
        with pytest.raises(expected.raises):
            await boundary.run(slot, subject.call(cause))
        return boundary.outcome

    def _assert_type(self, value: object, expected_type: type[BaseException] | None) -> None:
        if expected_type is None:
            assert value is None
        else:
            assert type(value) is expected_type

    @pytest.mark.parametrize(("cause", "slot", "expected"), _CELLS)
    async def test_cell(self, cause: Cause, slot: Slot, expected: Expected) -> None:
        outcome = await self._classify(cause, slot, expected)
        if not expected.records:
            assert outcome is None
            return
        assert outcome is not None
        assert outcome.slot is slot
        assert outcome.node == NODE
        assert outcome.state == expected.state
        self._assert_type(outcome.failure, expected.failure)
        self._assert_type(outcome.swallowed, expected.swallowed)


class TestVerdictEnforcement:
    async def test_the_return_split_has_exactly_two_branches(self) -> None:
        # Anything that is not the sentinel takes the same path, whatever it is.
        impostors: tuple[object, ...] = (None, "PASS", 42, True, [], {"verdict": "PASS"})
        for impostor in impostors:
            boundary = ChildBoundary(NODE)
            outcome = await boundary.run(Slot.TEST_METHOD, self._returns(impostor))
            assert outcome.state is State.ABORTED
            assert isinstance(outcome.failure, MissingVerdict)
            assert outcome.failure.returned == impostor

    async def test_a_non_pass_return_is_aborted_and_never_fail(self) -> None:
        boundary = ChildBoundary(NODE)
        outcome = await boundary.run(Slot.TEST_METHOD, self._returns(None))
        assert outcome.state is State.ABORTED
        assert not isinstance(outcome.failure, TestFailure)
        assert outcome.is_bug is True

    async def test_the_returned_value_is_kept_where_no_verdict_is_demanded(self) -> None:
        boundary = ChildBoundary(NODE)
        ctx = {"serial": "SN-0042"}
        outcome = await boundary.run(Slot.CONFIGURE, self._returns(ctx))
        assert outcome.state is None
        assert outcome.value == ctx

    async def _returns(self, value: object) -> object:
        return value


class TestBugGuardLayers:
    touched_hardware = False

    async def test_the_developer_layer_fires_on_a_stray_exception(self) -> None:
        boundary = ChildBoundary(NODE)
        outcome = await boundary.run(Slot.TEST_METHOD, self._raises(ValueError("kaboom")))
        assert isinstance(outcome.failure, ContractViolation)
        assert not isinstance(outcome.failure, FrameworkContractViolation)
        assert outcome.failure.BANNER == "CONTRACT VIOLATION"
        assert outcome.failure.cause is not None
        assert isinstance(outcome.failure.cause, ValueError)

    async def test_the_framework_layer_fires_on_a_cancellation_nobody_asked_for(self) -> None:
        boundary = ChildBoundary(NODE)
        with pytest.raises(asyncio.CancelledError):
            await boundary.run(Slot.TEST_METHOD, self._raises(asyncio.CancelledError()))
        outcome = boundary.outcome
        assert outcome is not None
        assert isinstance(outcome.failure, FrameworkContractViolation)
        assert outcome.failure.BANNER == "FRAMEWORK CONTRACT VIOLATION"

    async def test_the_two_layers_are_distinguishable_in_the_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("ERROR", logger="litmus"):
            developer = ChildBoundary(NODE)
            await developer.run(Slot.TEST_METHOD, self._raises(ValueError("kaboom")))
            framework = ChildBoundary(NODE)
            with pytest.raises(asyncio.CancelledError):
                await framework.run(Slot.TEST_METHOD, self._raises(asyncio.CancelledError()))
        banners = [record.getMessage().split(" IN ")[0] for record in caplog.records]
        assert "CONTRACT VIOLATION" in banners
        assert "FRAMEWORK CONTRACT VIOLATION" in banners

    async def test_an_undeclared_slot_is_a_framework_bug_caught_before_hardware(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Unreachable while `Slot` and `SLOT_POLICIES` agree — which is itself a test.
        # The guard exists so that if they ever diverge, the framework says so instead
        # of calling into suite code under a policy it does not have.
        monkeypatch.delitem(SLOT_POLICIES, Slot.AUDIT)
        boundary = ChildBoundary(NODE)
        call = self._never_runs()
        with pytest.raises(FrameworkContractViolation) as raised:
            await boundary.run(Slot.AUDIT, call)
        assert raised.value.BANNER == "FRAMEWORK CONTRACT VIOLATION"
        assert self.touched_hardware is False
        call.close()

    async def _never_runs(self) -> object:
        self.touched_hardware = True
        return PASS

    async def _raises(self, error: BaseException) -> object:
        raise error


class TestIntegrationFailureIsNeverRewrapped:
    async def test_it_crosses_every_slot_as_the_same_object(self) -> None:
        for slot in Slot:
            original = ExternalIntegrationFailure("before_each", HOOK_CAUSE)
            boundary = ChildBoundary(NODE)
            with pytest.raises(ExternalIntegrationFailure) as raised:
                await boundary.run(slot, self._raises(original))
            assert raised.value is original
            assert boundary.outcome is not None
            assert boundary.outcome.failure is original

    async def test_a_hook_failure_wraps_the_integrators_error_verbatim(self) -> None:
        cause = RuntimeError("shop-floor database refused the record")
        boundary = ChildBoundary(NODE)
        with pytest.raises(ExternalIntegrationFailure) as raised:
            await boundary.run(Slot.AFTER_EACH, self._raises(cause))
        assert raised.value.hook == "after_each"
        assert raised.value.cause is cause

    async def test_a_hook_failure_names_the_hook_that_threw(self) -> None:
        boundary = ChildBoundary(NODE)
        with pytest.raises(ExternalIntegrationFailure) as raised:
            await boundary.run(Slot.BEFORE_EACH, self._raises(RuntimeError("db down")))
        assert raised.value.hook == "before_each"

    async def test_evidence_slots_do_not_swallow_the_halt_signal(self) -> None:
        # Cleanup and survey swallow errors to protect the verdict. Swallowing the halt
        # signal would let the run continue past its own abort.
        for slot in (Slot.CLEANUP, Slot.SURVEY):
            original = ExternalIntegrationFailure("before_each", HOOK_CAUSE)
            boundary = ChildBoundary(NODE)
            with pytest.raises(ExternalIntegrationFailure):
                await boundary.run(slot, self._raises(original))

    async def _raises(self, error: BaseException) -> object:
        raise error


class TestRunAbortSignal:
    def test_it_starts_unfired(self) -> None:
        abort = RunAbort()
        assert abort.fired is False
        assert abort.reason is None

    def test_the_first_reason_wins(self) -> None:
        # A later hook failing during the teardown the first one caused must not
        # overwrite the explanation of why the run stopped.
        abort = RunAbort()
        first = ExternalIntegrationFailure("before_each", RuntimeError("db down"))
        second = ExternalIntegrationFailure("after_each", RuntimeError("also down"))
        abort.fire(first)
        abort.fire(second)
        assert abort.reason is first

    async def test_a_hook_failure_fires_it(self) -> None:
        abort = RunAbort()
        boundary = ChildBoundary(NODE, abort)
        with pytest.raises(ExternalIntegrationFailure):
            await boundary.run(Slot.BEFORE_EACH, self._raises(RuntimeError("db down")))
        assert abort.fired is True

    async def test_a_test_failure_does_not_fire_it(self) -> None:
        abort = RunAbort()
        boundary = ChildBoundary(NODE, abort)
        await boundary.run(Slot.TEST_METHOD, self._raises(TestFailure("post failed")))
        assert abort.fired is False

    async def _raises(self, error: BaseException) -> object:
        raise error


class TestTaskGroupSemantics:
    """A rack is seventy DUTs in one TaskGroup. What escapes the boundary decides
    whether one bad unit is data or a run-ending event."""

    RACK_SIZE = 70
    BAD_UNIT = 12

    async def test_a_failing_child_does_not_disturb_its_siblings(self) -> None:
        outcomes: dict[int, Outcome] = {}
        async with asyncio.TaskGroup() as group:
            for index in range(self.RACK_SIZE):
                group.create_task(self._run_unit(index, outcomes, self._fails))
        assert len(outcomes) == self.RACK_SIZE
        assert outcomes[self.BAD_UNIT].state is State.FAIL
        passed = [index for index, outcome in outcomes.items() if outcome.state is State.PASS]
        assert len(passed) == self.RACK_SIZE - 1

    async def test_a_forgotten_return_does_not_disturb_its_siblings(self) -> None:
        # One author's missing `return` must not take down a seventy-DUT run.
        outcomes: dict[int, Outcome] = {}
        async with asyncio.TaskGroup() as group:
            for index in range(self.RACK_SIZE):
                group.create_task(self._run_unit(index, outcomes, self._forgets_to_return))
        assert outcomes[self.BAD_UNIT].state is State.ABORTED
        assert isinstance(outcomes[self.BAD_UNIT].failure, MissingVerdict)
        assert len(outcomes) == self.RACK_SIZE

    async def test_a_harness_abort_cancels_every_sibling(self) -> None:
        # The one thing that IS allowed to propagate. TaskGroup cancelling everything
        # is exactly right here, because that is what Aborted means.
        abort = RunAbort()
        boundaries: dict[int, ChildBoundary] = {}
        with pytest.raises(BaseExceptionGroup) as raised:
            await self._run_rack_until_halt(abort, boundaries)
        assert raised.group_contains(ExternalIntegrationFailure)
        assert abort.fired is True
        siblings = [
            boundaries[index].outcome
            for index in boundaries
            if index != self.BAD_UNIT and boundaries[index].outcome is not None
        ]
        assert siblings
        for outcome in siblings:
            assert outcome is not None
            assert outcome.state is State.ABORTED
            assert outcome.failure is abort.reason

    async def _run_rack_until_halt(
        self, abort: RunAbort, boundaries: dict[int, ChildBoundary]
    ) -> None:
        async with asyncio.TaskGroup() as group:
            for index in range(self.RACK_SIZE):
                group.create_task(self._run_halting_unit(index, abort, boundaries))

    async def _run_unit(
        self,
        index: int,
        outcomes: dict[int, Outcome],
        misbehave: Callable[[], Coroutine[Any, Any, object]],
    ) -> None:
        boundary = ChildBoundary(f"dut-{index}")
        call = misbehave() if index == self.BAD_UNIT else self._passes()
        outcomes[index] = await boundary.run(Slot.TEST_METHOD, call)

    async def _run_halting_unit(
        self, index: int, abort: RunAbort, boundaries: dict[int, ChildBoundary]
    ) -> None:
        boundary = ChildBoundary(f"dut-{index}", abort)
        boundaries[index] = boundary
        if index == self.BAD_UNIT:
            await boundary.run(Slot.BEFORE_EACH, self._raises(RuntimeError("db down")))
            return
        await boundary.run(Slot.TEST_METHOD, self._waits_for_hardware())

    async def _passes(self) -> object:
        await asyncio.sleep(0)
        return PASS

    async def _fails(self) -> object:
        raise TestFailure("POST failed", expected="pass", received="beep code 3")

    async def _forgets_to_return(self) -> object:
        return None

    async def _waits_for_hardware(self) -> object:
        await asyncio.sleep(30)
        return PASS

    async def _raises(self, error: BaseException) -> object:
        raise error
