"""The ruled per-node Time Budget Contract from issues #59 and #60."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable

import asyncssh
import pytest
from tests.loopback_sshd import LoopbackSshd
from tests.mock_connector import ConnectorInstaller

from litmus import (
    PASS,
    AfterEachContext,
    BeforeEachContext,
    ContractViolation,
    ExternalIntegrationFailure,
    LitmusTest,
    TestFailure,
    TestUtils,
    Verdict,
)
from litmus.base import (
    _DEADLINE_CANCELLATION,  # pyright: ignore[reportPrivateUsage]
    _cancel_for_deadline,  # pyright: ignore[reportPrivateUsage]
    _consume_deadline_cancellation,  # pyright: ignore[reportPrivateUsage]
)
from litmus.executor import ExecResult
from litmus.lifecycle import RunAbort, Slot, State, TimeBudget, TimeBudgetClock
from litmus.mock import MockTransport
from litmus.remote import RemoteInterface

_BUDGET = 0.03
_PROMPT = 1.0


class _BlockingCancelTransport(RemoteInterface):
    """Hold resource teardown so a second cancellation can collide with it."""

    def __init__(self) -> None:
        super().__init__()
        self.cancel_started = asyncio.Event()
        self.release_cancel = asyncio.Event()
        self.cancel_count = 0
        self.disconnect_count = 0

    async def _connect(self) -> None:
        pass

    async def _disconnect(self) -> None:
        self.disconnect_count += 1

    async def _exec(self, command: str) -> ExecResult:
        del command
        await asyncio.Event().wait()
        return ExecResult(stdout="", stderr="", exit_code=0)

    async def _cancel_in_flight(self) -> None:
        self.cancel_count += 1
        self.cancel_started.set()
        await self.release_cancel.wait()
        await self._disconnect()


class _OneClaim(LitmusTest):
    def __init__(
        self,
        *,
        name: str = "root",
        claim_fail_after: float | None = None,
        claim_abort_after: float | None = None,
        root_fail_after: float | None = None,
        root_abort_after: float | None = None,
        claim: Callable[[TestUtils], Awaitable[object]] | None = None,
    ) -> None:
        super().__init__(
            name=name,
            fail_after=root_fail_after,
            abort_after=root_abort_after,
        )
        self._claim_fail_after = claim_fail_after
        self._claim_abort_after = claim_abort_after
        self._claim_override = claim

    def build_suite(self) -> LitmusTest:
        return self.test(
            "claim",
            self._claim,
            fail_after=self._claim_fail_after,
            abort_after=self._claim_abort_after,
        )

    async def _claim(self, utils: TestUtils) -> Verdict:
        if self._claim_override is not None:
            await self._claim_override(utils)
        await asyncio.Event().wait()
        return PASS


class TestTimeBudgetValidation:
    @pytest.mark.parametrize("value", [True, 0, -1, math.inf, -math.inf, math.nan, "1"])
    def test_duration_must_be_a_positive_finite_number(self, value: object) -> None:
        with pytest.raises(ContractViolation, match="positive finite") as raised:
            LitmusTest(name="invalid", fail_after=value)  # pyright: ignore[reportArgumentType]
        # A violation raised at composition names the node it was declared on; a rack
        # of seventy declarations is otherwise a search.
        assert raised.value.node == "invalid"

    @pytest.mark.parametrize("declared", ["fail_after", "abort_after"])
    def test_huge_integer_is_a_contract_violation_at_composition(
        self,
        declared: str,
    ) -> None:
        huge = 10**1000
        construct = (
            (lambda: LitmusTest(name="invalid", fail_after=huge))
            if declared == "fail_after"
            else (lambda: LitmusTest(name="invalid", abort_after=huge))
        )

        with pytest.raises(ContractViolation, match="positive finite") as raised:
            construct()
        # float() refusing the integer is the evidence; the violation keeps it.
        assert raised.value.node == "invalid"
        assert isinstance(raised.value.cause, OverflowError)

    @pytest.mark.parametrize("declared", ["fail_after", "abort_after"])
    def test_huge_integer_is_a_contract_violation_at_a_composition_factory(
        self,
        declared: str,
    ) -> None:
        root = LitmusTest(name="root")

        async def claim(utils: TestUtils) -> Verdict:
            del utils
            return PASS

        compose = (
            (lambda: root.test("invalid", claim, fail_after=10**1000))
            if declared == "fail_after"
            else (lambda: root.test("invalid", claim, abort_after=10**1000))
        )

        with pytest.raises(ContractViolation, match="positive finite"):
            compose()

    @pytest.mark.parametrize("declared", ["fail_after", "abort_after"])
    async def test_huge_integer_run_budget_does_not_consume_the_instance(
        self,
        declared: str,
    ) -> None:
        suite = _OneClaim()
        invalid_run = (
            suite.run(fail_after=10**1000)
            if declared == "fail_after"
            else suite.run(abort_after=10**1000)
        )

        with pytest.raises(ContractViolation, match="positive finite"):
            await invalid_run

        result = await asyncio.wait_for(suite.run(abort_after=_BUDGET), _PROMPT)
        assert result.state is State.ABORTED

    def test_exactly_one_name_is_accepted_at_every_composition_factory(self) -> None:
        root = LitmusTest(name="root")

        async def claim(utils: TestUtils) -> Verdict:
            del utils
            return PASS

        with pytest.raises(ContractViolation, match="mutually exclusive"):
            root.test("case", claim, fail_after=1, abort_after=1)
        child = root.test("child", claim)
        with pytest.raises(ContractViolation, match="mutually exclusive"):
            root.parallel("group", [child], fail_after=1, abort_after=1)
        with pytest.raises(ContractViolation, match="mutually exclusive"):
            root.sequence("sequence", [child], fail_after=1, abort_after=1)

    async def test_invalid_run_budget_is_rejected_before_the_instance_is_consumed(self) -> None:
        async def yield_once(utils: TestUtils) -> None:
            del utils
            await asyncio.sleep(0)

        suite = _OneClaim(claim=yield_once)
        with pytest.raises(ContractViolation, match="mutually exclusive"):
            await suite.run(fail_after=1, abort_after=1)

        result = await asyncio.wait_for(suite.run(abort_after=_BUDGET), _PROMPT)
        assert result.state is State.ABORTED

    async def test_run_cannot_add_a_second_budget_to_the_root_node(self) -> None:
        suite = _OneClaim(root_abort_after=_BUDGET)

        with pytest.raises(ContractViolation, match="already declared"):
            await suite.run(fail_after=1)

        result = await asyncio.wait_for(suite.run(), _PROMPT)
        assert result.state is State.ABORTED


class TestTimeBudgetClock:
    async def test_one_departing_lineage_does_not_pause_an_active_peer(self) -> None:
        clock = TimeBudgetClock(TimeBudget("fail_after", _BUDGET), clock="root")
        first = object()
        second = object()
        clock.activate(first)
        clock.activate(second)

        clock.deactivate(first)

        await asyncio.wait_for(clock.event.wait(), _PROMPT)
        assert clock.fired

    async def test_clock_stalls_only_after_its_last_active_lineage_leaves(self) -> None:
        clock = TimeBudgetClock(TimeBudget("fail_after", _BUDGET), clock="root")
        first = object()
        second = object()

        assert clock.state is State.FAIL
        assert clock.fired_at is None
        with pytest.raises(RuntimeError, match="before the clock fires"):
            clock.evidence()

        clock.activate(first)
        clock.activate(first)
        clock.activate(second)
        clock.deactivate(first)
        clock.deactivate(second)
        clock.deactivate(second)
        await asyncio.sleep(_BUDGET * 2)
        assert not clock.event.is_set()
        clock.close()
        clock.close()
        clock.activate(first)
        clock.deactivate(first)

        active = TimeBudgetClock(TimeBudget("fail_after", 1), clock="active")
        active.activate(object())
        active.close()

        assert not clock.fired

    async def test_expiry_is_one_way_and_renders_evidence(self) -> None:
        clock = TimeBudgetClock(TimeBudget("abort_after", 0.001), clock="root/child")
        lineage = object()
        clock.activate(lineage)

        await asyncio.wait_for(clock.event.wait(), _PROMPT)
        evidence = clock.evidence()
        clock.activate(object())
        clock.deactivate(lineage)
        clock._expire()  # pyright: ignore[reportPrivateUsage]
        clock.close()

        assert clock.state is State.ABORTED
        assert clock.fired
        assert clock.fired_at is not None
        assert evidence.to_json() == {
            "reason": "timeout",
            "declared": "abort_after",
            "budget_seconds": 0.001,
            "elapsed_seconds": evidence.elapsed_seconds,
            "clock": "root/child",
        }

    async def test_activate_expires_an_already_consumed_budget(self) -> None:
        clock = TimeBudgetClock(TimeBudget("fail_after", 1), clock="root")
        object.__setattr__(clock, "_consumed", 1.0)
        clock._expire()  # pyright: ignore[reportPrivateUsage]

        assert not clock.fired
        clock.activate(object())

        assert clock.fired


class TestTimeBudgetStatesAndEvidence:
    async def test_fail_after_is_a_structured_fail(self) -> None:
        result = await asyncio.wait_for(
            _OneClaim(claim_fail_after=_BUDGET).run(),
            _PROMPT,
        )
        claim = result.summary_tree.children[0]

        assert result.state is State.FAIL
        assert claim.state is State.FAIL
        assert isinstance(claim.failure, TestFailure)
        assert claim.timeout is not None
        assert claim.timeout.reason == "timeout"
        assert claim.timeout.declared == "fail_after"
        assert claim.timeout.budget_seconds == _BUDGET
        assert claim.timeout.elapsed_seconds >= _BUDGET
        assert claim.timeout.clock == "root/claim"
        assert result.to_json()["summary_tree"]["children"][0]["timeout"] == (
            claim.timeout.to_json()
        )

    async def test_abort_after_is_node_local_and_forges_no_failure(self) -> None:
        sibling_finished = asyncio.Event()

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "claims",
                    [
                        self.test("parked", self._parked, abort_after=_BUDGET),
                        self.test("sibling", self._sibling),
                    ],
                )

            async def _parked(self, utils: TestUtils) -> Verdict:
                del utils
                await asyncio.Event().wait()
                return PASS

            async def _sibling(self, utils: TestUtils) -> Verdict:
                del utils
                await asyncio.sleep(_BUDGET * 2)
                sibling_finished.set()
                return PASS

        result = await asyncio.wait_for(Suite(name="root").run(), _PROMPT)
        group = result.summary_tree.children[0]

        assert result.state is State.ABORTED
        assert group.children[0].state is State.ABORTED
        assert group.children[0].failure is None
        assert group.children[0].timeout is not None
        assert group.children[0].timeout.declared == "abort_after"
        assert group.children[1].state is State.PASS
        assert sibling_finished.is_set()

    async def test_descendant_clock_tightens_run_budget_and_fires_first(self) -> None:
        result = await asyncio.wait_for(
            _OneClaim(claim_fail_after=_BUDGET).run(abort_after=0.3),
            _PROMPT,
        )
        claim = result.summary_tree.children[0]

        assert result.state is State.FAIL
        assert claim.state is State.FAIL
        assert claim.timeout is not None
        assert claim.timeout.clock == "root/claim"
        assert claim.timeout.declared == "fail_after"

    async def test_ancestor_clock_bounds_longer_descendant_and_names_itself(self) -> None:
        result = await asyncio.wait_for(
            _OneClaim(claim_fail_after=0.3).run(abort_after=_BUDGET),
            _PROMPT,
        )
        claim = result.summary_tree.children[0]

        assert result.state is State.ABORTED
        assert claim.state is State.ABORTED
        assert claim.timeout is not None
        assert claim.timeout.clock == "root"
        assert claim.timeout.declared == "abort_after"

    async def test_node_budget_preempts_a_longer_wait_for_timeout(self) -> None:
        async def wait_for_never_ready(utils: TestUtils) -> None:
            await utils.wait_for(
                lambda: False,
                looking_for="DUT ready",
                interval=0.1,
                timeout_seconds=1,
            )

        result = await asyncio.wait_for(
            _OneClaim(
                claim_fail_after=_BUDGET,
                claim=wait_for_never_ready,
            ).run(),
            _PROMPT,
        )
        claim = result.summary_tree.children[0]

        assert claim.state is State.FAIL
        assert claim.timeout is not None
        assert claim.timeout.declared == "fail_after"
        assert isinstance(claim.failure, TestFailure)
        assert claim.failure.expected == {"budget_seconds": _BUDGET}


class TestTimeBudgetWindowsAndComposition:
    async def test_unmarked_caller_cancellation_propagates_through_a_parallel_node(self) -> None:
        both_started = asyncio.Event()
        started = 0

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "group",
                    [self.test("one", self._park), self.test("two", self._park)],
                )

            async def _park(self, utils: TestUtils) -> Verdict:
                nonlocal started
                del utils
                started += 1
                if started == 2:
                    both_started.set()
                await asyncio.Event().wait()
                return PASS

        running = asyncio.create_task(Suite(name="root").run())
        await asyncio.wait_for(both_started.wait(), _PROMPT)

        running.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, _PROMPT)

    async def test_integration_abort_outranks_a_same_turn_deadline(self) -> None:
        suite = _OneClaim()
        run_abort = RunAbort()
        clock = TimeBudgetClock(TimeBudget("fail_after", _PROMPT), clock="root")
        failure = ExternalIntegrationFailure("before_each", RuntimeError("integration lost"))

        async def collide(
            active_clocks: tuple[TimeBudgetClock, ...],
            lineage: object,
        ) -> object:
            active_clocks[0].activate(lineage)
            run_abort.fire(failure)
            clock._expire()  # pyright: ignore[reportPrivateUsage]
            raise failure

        with pytest.raises(ExternalIntegrationFailure) as raised:
            await suite._run_timed_window(  # pyright: ignore[reportPrivateUsage]
                TestUtils(),
                run_abort,
                (clock,),
                (),
                collide,
            )

        assert raised.value is failure

    async def test_configure_is_inside_but_cleanup_is_outside_the_window(self) -> None:
        cleaned = asyncio.Event()

        class ConfigureParks(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                del utils
                await asyncio.Event().wait()
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("unreached", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                del utils
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                del utils
                await asyncio.sleep(_BUDGET * 2)
                cleaned.set()

        result = await asyncio.wait_for(
            ConfigureParks(name="root").run(fail_after=_BUDGET),
            _PROMPT,
        )

        assert result.state is State.FAIL
        assert result.slot is Slot.CONFIGURE
        assert result.timeout is not None
        assert cleaned.is_set()

    async def test_cleanup_and_all_root_owned_integration_hooks_are_excluded(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                del utils
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                del utils
                await asyncio.sleep(_BUDGET * 2)

        async def slow_before(ctx: BeforeEachContext) -> None:
            del ctx
            await asyncio.sleep(_BUDGET * 2)

        async def slow_after(ctx: AfterEachContext) -> None:
            del ctx
            await asyncio.sleep(_BUDGET * 2)

        result = await asyncio.wait_for(
            Suite(name="root").run(
                fail_after=_BUDGET,
                before_each=slow_before,
                after_each=slow_after,
            ),
            _PROMPT,
        )

        assert result.state is State.PASS
        assert result.timeout is None

    async def test_sibling_before_hook_cannot_pause_an_active_ancestor_lineage(self) -> None:
        parked_cut = asyncio.Event()

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "bounded group",
                    [
                        self.test("slow hook", self._pass),
                        self.test("parked", self._park),
                    ],
                    fail_after=0.05,
                )

            async def _pass(self, utils: TestUtils) -> Verdict:
                del utils
                return PASS

            async def _park(self, utils: TestUtils) -> Verdict:
                del utils
                try:
                    await asyncio.Event().wait()
                finally:
                    parked_cut.set()
                return PASS

        async def slow_one_sibling(ctx: BeforeEachContext) -> None:
            if ctx.name == "slow hook":
                await asyncio.sleep(0.3)

        started_at = asyncio.get_running_loop().time()
        running = asyncio.create_task(
            Suite(name="root").run(
                before_each=slow_one_sibling,
                allow_unpaired_hooks=True,
            )
        )

        await asyncio.wait_for(parked_cut.wait(), 0.2)
        cut_after = asyncio.get_running_loop().time() - started_at
        result = await asyncio.wait_for(running, _PROMPT)

        assert cut_after < 0.2
        assert result.state is State.FAIL

    async def test_sibling_cleanup_cannot_pause_an_active_ancestor_lineage(self) -> None:
        cleanup_started = asyncio.Event()
        parked_cut = asyncio.Event()

        class FinishesThenCleans(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("passes", self._pass)

            async def _pass(self, utils: TestUtils) -> Verdict:
                del utils
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                del utils
                cleanup_started.set()
                await asyncio.sleep(0.3)

        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "bounded group",
                    [FinishesThenCleans(name="cleaner"), self.test("parked", self._park)],
                    fail_after=0.05,
                )

            async def _park(self, utils: TestUtils) -> Verdict:
                del utils
                try:
                    await asyncio.Event().wait()
                finally:
                    parked_cut.set()
                return PASS

        started_at = asyncio.get_running_loop().time()
        running = asyncio.create_task(Suite(name="root").run())

        await asyncio.wait_for(cleanup_started.wait(), 0.2)
        await asyncio.wait_for(parked_cut.wait(), 0.2)
        cut_after = asyncio.get_running_loop().time() - started_at
        result = await asyncio.wait_for(running, _PROMPT)

        assert cut_after < 0.2
        assert result.state is State.FAIL

    async def test_parallel_cut_preserves_settled_sibling_and_sequence_leaves_pending_tail(
        self,
    ) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                group = self.parallel(
                    "bounded group",
                    [self.test("settled", self._pass), self.test("parked", self._park)],
                    fail_after=_BUDGET,
                )
                sequence = self.sequence(
                    "bounded sequence",
                    [self.test("parked first", self._park), self.test("never", self._pass)],
                    abort_after=_BUDGET,
                )
                return self.sequence("root sequence", [group, sequence])

            async def _pass(self, utils: TestUtils) -> Verdict:
                del utils
                return PASS

            async def _park(self, utils: TestUtils) -> Verdict:
                del utils
                await asyncio.Event().wait()
                return PASS

        first = await asyncio.wait_for(Suite(name="root").run(), _PROMPT)
        group = first.summary_tree.children[0].children[0]

        assert first.state is State.FAIL
        assert group.state is State.FAIL
        assert group.children[0].state is State.PASS
        assert group.children[1].state is State.FAIL

        class SequenceOnly(Suite):
            def build_suite(self) -> LitmusTest:
                return self.sequence(
                    "bounded sequence",
                    [self.test("parked first", self._park), self.test("never", self._pass)],
                    abort_after=_BUDGET,
                )

        second = await asyncio.wait_for(SequenceOnly(name="root").run(), _PROMPT)
        sequence = second.summary_tree.children[0]
        assert sequence.state is State.ABORTED
        assert sequence.children[0].state is State.ABORTED
        assert sequence.children[1].state is None

    async def test_settled_aborted_child_outranks_later_parent_fail_cut(self) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.parallel(
                    "bounded group",
                    [
                        self.test("aborts first", self._park, abort_after=_BUDGET),
                        self.test("still parked", self._park),
                    ],
                    fail_after=_BUDGET * 2,
                )

            async def _park(self, utils: TestUtils) -> Verdict:
                del utils
                await asyncio.Event().wait()
                return PASS

        result = await asyncio.wait_for(Suite(name="root").run(), _PROMPT)
        group = result.summary_tree.children[0]

        assert group.state is State.ABORTED
        assert group.timeout is not None
        assert group.timeout.declared == "fail_after"
        assert group.children[0].state is State.ABORTED
        assert group.children[0].timeout is not None
        assert group.children[0].timeout.declared == "abort_after"
        assert group.children[1].state is State.FAIL


class TestTimeBudgetResourceCancellation:
    async def test_caller_cancellation_during_deadline_teardown_is_restored(
        self,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        transport = _BlockingCancelTransport()

        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, transport)
                await utils.use_shell(host="dut", username="root")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("parked", self._park)

            async def _park(self, utils: TestUtils) -> Verdict:
                await utils.exec("park")
                return PASS

        suite = Suite(name="root")
        running = asyncio.create_task(suite.run(fail_after=_BUDGET))
        await asyncio.wait_for(transport.cancel_started.wait(), _PROMPT)

        running.cancel()
        transport.release_cancel.set()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, _PROMPT)
        assert transport.cancel_count == 1
        assert transport.disconnect_count == 1
        assert not transport.connected
        assert suite._summary_tree is None  # pyright: ignore[reportPrivateUsage]

    async def test_mock_transport_is_hard_cancelled_at_owning_node_scope(
        self,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        transport = MockTransport()
        transport.park("park")

        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, transport)
                await utils.use_shell(host="dut", username="root")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("parked", self._park)

            async def _park(self, utils: TestUtils) -> Verdict:
                await utils.exec("park")
                return PASS

        result = await asyncio.wait_for(Suite(name="root").run(fail_after=_BUDGET), _PROMPT)

        assert result.state is State.FAIL
        assert transport.cancel_count == 1
        assert not transport.connected
        assert not transport.has_in_flight

    async def test_fail_after_bounds_a_via_configure_and_consumer_adopts_evidence(
        self,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        transport = MockTransport()
        cleaned = asyncio.Event()

        class Via(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, transport)
                await utils.use_shell(host="gateway", username="root")
                await asyncio.Event().wait()
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("unused", self._pass)

            async def _pass(self, utils: TestUtils) -> Verdict:
                del utils
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                del utils
                assert not transport.connected
                cleaned.set()

        class Consumer(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("unreached", self._pass)

            async def _pass(self, utils: TestUtils) -> Verdict:
                del utils
                return PASS

        result = await asyncio.wait_for(
            Consumer(
                name="consumer",
                via=Via(name="gateway", fail_after=_BUDGET),
            ).run(),
            _PROMPT,
        )

        assert result.state is State.FAIL
        assert result.slot is Slot.CONFIGURE
        assert isinstance(result.failure, TestFailure)
        assert result.timeout is not None
        assert result.timeout.declared == "fail_after"
        assert result.timeout.clock == "consumer/gateway"
        assert transport.cancel_count == 1
        assert cleaned.is_set()

    async def test_abort_after_on_via_preserves_borrowed_hop_until_cleanup(
        self,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        hop = MockTransport()
        target = MockTransport()
        cleanup_topology: list[tuple[bool, bool]] = []

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, {"gateway": hop})
                await utils.use_shell(host="gateway", username="root")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("unused", self._pass)

            async def _pass(self, utils: TestUtils) -> Verdict:
                del utils
                return PASS

        class TimedVia(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, {"target": target})
                await utils.use_shell(host="target", username="root")
                await asyncio.Event().wait()
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("unused", self._pass)

            async def _pass(self, utils: TestUtils) -> Verdict:
                del utils
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                del utils
                cleanup_topology.append((target.connected, hop.connected))

        class Consumer(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("unreached", self._pass)

            async def _pass(self, utils: TestUtils) -> Verdict:
                del utils
                return PASS

        gateway = Gateway(name="gateway")
        timed_via = TimedVia(
            name="timed via",
            via=gateway,
            abort_after=_BUDGET,
        )
        result = await asyncio.wait_for(
            Consumer(name="consumer", via=timed_via).run(),
            _PROMPT,
        )

        assert result.state is State.ABORTED
        assert result.failure is None
        assert result.timeout is not None
        assert result.timeout.declared == "abort_after"
        assert result.timeout.clock == "consumer/timed via"
        assert target.cancel_count == 1
        assert hop.cancel_count == 0
        assert cleanup_topology == [(False, True)]
        assert hop.disconnect_count == 1

    async def test_public_configure_only_maps_deadline_abort_to_timeout(self) -> None:
        cleaned = asyncio.Event()

        class Via(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                del utils
                await asyncio.Event().wait()
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("unused", self._pass)

            async def _pass(self, utils: TestUtils) -> Verdict:
                del utils
                return PASS

            async def cleanup(self, utils: TestUtils) -> None:
                del utils
                cleaned.set()

        via = Via(name="gateway", abort_after=_BUDGET)
        with pytest.raises(TimeoutError, match="abort_after"):
            await asyncio.wait_for(via.run_configure_only(), _PROMPT)
        await via.run_cleanup_only()

        assert cleaned.is_set()

    async def test_public_configure_only_caller_cancel_still_cancels_bring_up(self) -> None:
        configure_started = asyncio.Event()
        configure_cancelled = asyncio.Event()

        class Via(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                del utils
                configure_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    configure_cancelled.set()
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("unused", self._pass)

            async def _pass(self, utils: TestUtils) -> Verdict:
                del utils
                return PASS

        via = Via(name="gateway")
        running = asyncio.create_task(via.run_configure_only())
        await asyncio.wait_for(configure_started.wait(), _PROMPT)

        running.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, _PROMPT)
        await asyncio.wait_for(configure_cancelled.wait(), _PROMPT)
        configure_task = via._configure_only_task  # pyright: ignore[reportPrivateUsage]
        assert configure_task is not None
        await asyncio.gather(configure_task, return_exceptions=True)
        await via.run_cleanup_only()

    async def test_deadline_cancels_owned_tunnel_but_never_its_borrowed_hop(
        self,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        hop = MockTransport()
        target = MockTransport()
        target.park("park")

        class Gateway(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, {"gateway": hop})
                await utils.use_shell(host="gateway", username="root")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("unused", self._pass)

            async def _pass(self, utils: TestUtils) -> Verdict:
                del utils
                return PASS

        class Consumer(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, {"target": target})
                await utils.use_shell(host="target", username="root")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("parked", self._park)

            async def _park(self, utils: TestUtils) -> Verdict:
                await utils.exec("park")
                return PASS

        gateway = Gateway(name="gateway")
        result = await asyncio.wait_for(
            Consumer(name="consumer", via=gateway, abort_after=_BUDGET).run(),
            _PROMPT,
        )

        assert result.state is State.ABORTED
        assert target.cancel_count == 1
        assert hop.cancel_count == 0
        assert hop.disconnect_count == 1

    async def test_loopback_sshd_exec_is_aborted_at_the_socket(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        peer = await LoopbackSshd.start()
        abort_calls = 0
        original = asyncssh.SSHClientConnection.abort

        def tracked_abort(connection: asyncssh.SSHClientConnection) -> None:
            nonlocal abort_calls
            abort_calls += 1
            original(connection)

        monkeypatch.setattr(asyncssh.SSHClientConnection, "abort", tracked_abort)

        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                await utils.use_shell(
                    host="127.0.0.1",
                    port=peer.port,
                    username=peer.username,
                    password=peer.password,
                    skip_host_verification=True,
                )
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("parked", self._park)

            async def _park(self, utils: TestUtils) -> Verdict:
                await utils.exec("sleep 30")
                return PASS

        try:
            result = await asyncio.wait_for(
                Suite(name="root").run(fail_after=0.2),
                _PROMPT,
            )
        finally:
            await peer.aclose()

        assert result.state is State.FAIL
        assert abort_calls == 1
        assert peer.connections == 1


class TestDeadlineCancellationOwnership:
    """The marker alone proves nothing; the recorded request count is what is owned.

    A deadline cut cancels a task with a private marker and records how many of that
    task's cancellation requests are the framework's. Consumption takes back exactly
    those, so an operator's or harness's cancel landing on the same task keeps
    travelling instead of being read as "the budget expired".
    """

    @staticmethod
    async def _park(seen: list[tuple[bool, int]]) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            task = asyncio.current_task()
            assert task is not None
            seen.append((_consume_deadline_cancellation(error), task.cancelling()))

    async def test_a_recorded_request_is_consumed_and_the_task_is_no_longer_cancelling(
        self,
    ) -> None:
        seen: list[tuple[bool, int]] = []
        task = asyncio.create_task(self._park(seen))
        await asyncio.sleep(0)

        _cancel_for_deadline(task)
        await task

        assert seen == [(True, 0)]

    async def test_a_marker_without_a_recorded_request_is_not_ours(self) -> None:
        async def forge() -> tuple[bool, int]:
            try:
                raise asyncio.CancelledError(_DEADLINE_CANCELLATION)
            except asyncio.CancelledError as error:
                task = asyncio.current_task()
                assert task is not None
                return _consume_deadline_cancellation(error), task.cancelling()

        assert await asyncio.create_task(forge()) == (False, 0)

    async def test_an_external_cancel_colliding_with_the_deadline_keeps_travelling(
        self,
    ) -> None:
        seen: list[tuple[bool, int]] = []
        task = asyncio.create_task(self._park(seen))
        await asyncio.sleep(0)

        _cancel_for_deadline(task)
        task.cancel()
        await task

        # One request was the framework's and is taken back; the other is not its to
        # remove, so the task is still cancelling when the handler returns.
        assert seen == [(False, 1)]

    async def test_outside_any_task_the_marker_belongs_to_nobody(self) -> None:
        loop = asyncio.get_running_loop()
        consumed: asyncio.Future[bool] = loop.create_future()
        error = asyncio.CancelledError(_DEADLINE_CANCELLATION)

        loop.call_soon(lambda: consumed.set_result(_consume_deadline_cancellation(error)))

        assert await consumed is False
