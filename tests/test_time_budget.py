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
from litmus.lifecycle import RunAbort, Slot, State, TimeBudget, TimeBudgetClock
from litmus.mock import MockTransport

_BUDGET = 0.03
_PROMPT = 1.0


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
        return self.case(
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
        with pytest.raises(ContractViolation, match="positive finite"):
            LitmusTest(name="invalid", fail_after=value)  # pyright: ignore[reportArgumentType]

    def test_exactly_one_name_is_accepted_at_every_composition_factory(self) -> None:
        root = LitmusTest(name="root")

        async def claim(utils: TestUtils) -> Verdict:
            del utils
            return PASS

        with pytest.raises(ContractViolation, match="mutually exclusive"):
            root.case("case", claim, fail_after=1, abort_after=1)
        child = root.case("child", claim)
        with pytest.raises(ContractViolation, match="mutually exclusive"):
            root.group("group", [child], fail_after=1, abort_after=1)
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
    async def test_pause_resume_and_close_exclude_time_without_firing(self) -> None:
        clock = TimeBudgetClock(TimeBudget("fail_after", 0.2), clock="root")

        assert clock.state is State.FAIL
        assert clock.fired_at is None
        with pytest.raises(RuntimeError, match="before the clock fires"):
            clock.evidence()

        clock.pause()
        clock.pause()
        clock._consume_active_time()  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.01)
        assert not clock.event.is_set()
        clock.resume()
        clock.resume()
        clock.close()
        clock.close()
        clock.resume()

        assert not clock.fired

    async def test_expiry_is_one_way_and_renders_evidence(self) -> None:
        clock = TimeBudgetClock(TimeBudget("abort_after", 0.001), clock="root/child")

        await asyncio.wait_for(clock.event.wait(), _PROMPT)
        evidence = clock.evidence()
        clock.pause()
        clock.resume()
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

    async def test_resume_expires_a_budget_consumed_while_it_was_being_paused(self) -> None:
        clock = TimeBudgetClock(TimeBudget("fail_after", 1), clock="root")
        clock.pause()
        object.__setattr__(clock, "_consumed", 1.0)
        clock._expire()  # pyright: ignore[reportPrivateUsage]

        assert not clock.fired
        clock.resume()

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
                return self.group(
                    "claims",
                    [
                        self.case("parked", self._parked, abort_after=_BUDGET),
                        self.case("sibling", self._sibling),
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
    async def test_integration_abort_outranks_a_same_turn_deadline(self) -> None:
        suite = _OneClaim()
        run_abort = RunAbort()
        clock = TimeBudgetClock(TimeBudget("fail_after", _PROMPT), clock="root")
        failure = ExternalIntegrationFailure("before_each", RuntimeError("integration lost"))

        async def collide(active_clocks: tuple[TimeBudgetClock, ...]) -> object:
            del active_clocks
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
                return self.case("unreached", self._claim)

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
                return self.case("claim", self._claim)

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

    async def test_group_cut_preserves_settled_sibling_and_sequence_leaves_pending_tail(
        self,
    ) -> None:
        class Suite(LitmusTest):
            def build_suite(self) -> LitmusTest:
                group = self.group(
                    "bounded group",
                    [self.case("settled", self._pass), self.case("parked", self._park)],
                    fail_after=_BUDGET,
                )
                sequence = self.sequence(
                    "bounded sequence",
                    [self.case("parked first", self._park), self.case("never", self._pass)],
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
                    [self.case("parked first", self._park), self.case("never", self._pass)],
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
                return self.group(
                    "bounded group",
                    [
                        self.case("aborts first", self._park, abort_after=_BUDGET),
                        self.case("still parked", self._park),
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
                return self.case("parked", self._park)

            async def _park(self, utils: TestUtils) -> Verdict:
                await utils.exec("park")
                return PASS

        result = await asyncio.wait_for(Suite(name="root").run(fail_after=_BUDGET), _PROMPT)

        assert result.state is State.FAIL
        assert transport.cancel_count == 1
        assert not transport.connected
        assert not transport.has_in_flight

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
                return self.case("unused", self._pass)

            async def _pass(self, utils: TestUtils) -> Verdict:
                del utils
                return PASS

        class Consumer(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, {"target": target})
                await utils.use_shell(host="target", username="root")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.case("parked", self._park)

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
                return self.case("parked", self._park)

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
