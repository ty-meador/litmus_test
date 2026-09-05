"""Rack-scale concurrency, isolation, memory, and abort measurements."""

from __future__ import annotations

import asyncio
import gc
import os
import signal
import tracemalloc
from collections.abc import Sequence
from dataclasses import dataclass

from tests.mock_connector import ConnectorInstaller

from litmus import (
    PASS,
    BeforeEachContext,
    LitmusTest,
    OperatorInterrupt,
    RunResult,
    SummaryTreeNode,
    TestFailure,
    TestUtils,
    Verdict,
)
from litmus.executor import ExecResult
from litmus.lifecycle import State
from litmus.mock import MockTransport

_DUT_COUNT = 70
_MEMORY_ROUNDS = 4
_RETAINED_MEMORY_LIMIT_BYTES = 256 * 1024
_ABORT_LATENCY_LIMIT_SECONDS = 1.0
_PARKED_DUT_BUDGET_SECONDS = 0.1
_DEADLINE_LATENCY_LIMIT_SECONDS = 0.5
_WAIT_LIMIT_SECONDS = 3.0


class _RackCoordinator:
    """Hold synthetic DUTs at a fair-start barrier and record settlements."""

    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.started: set[int] = set()
        self.completed: set[int] = set()
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()

    async def rendezvous(self, dut_id: int) -> None:
        """Record one DUT as runnable, then wait for the rack-wide release."""
        self.started.add(dut_id)
        if len(self.started) == self.expected:
            self.all_started.set()
        await self.release.wait()


@dataclass(slots=True)
class _AbortMeasurement:
    """Capture the event-loop instant at which the harness abort begins."""

    requested_at: float | None = None


class _SyntheticDut(LitmusTest):
    """Represent one independently routed DUT in the synthetic rack."""

    def __init__(
        self,
        *,
        dut_id: int,
        transport: MockTransport,
        coordinator: _RackCoordinator,
        install: ConnectorInstaller,
        include_abort_trigger: bool = False,
        fail_after: float | None = None,
    ) -> None:
        super().__init__(name=f"dut-{dut_id:02d}", fail_after=fail_after)
        self._dut_id = dut_id
        self._transport = transport
        self._coordinator = coordinator
        self._install = install
        self._include_abort_trigger = include_abort_trigger

    async def configure(self, utils: TestUtils) -> dict[str, object]:
        """Connect this DUT to its private mock transport, and only to that one."""
        host = self.name
        self._install(utils, {host: self._transport})
        await utils.use_shell(host=host, username="root")
        return {}

    def build_suite(self) -> LitmusTest:
        """Run the DUT probe and, for one peer, the harness-abort trigger."""
        tests = [self.case("probe", self._probe)]
        if self._include_abort_trigger:
            tests.append(self.case("trigger whole-rack abort", self._abort_trigger))
        return self.group(f"{self.name} claims", tests)

    async def _probe(self, utils: TestUtils) -> Verdict:
        await self._coordinator.rendezvous(self._dut_id)
        result = await utils.exec("probe")
        if result.exit_code != 0:
            raise TestFailure(
                "synthetic DUT probe failed",
                f"exit code 0 from {self.name}",
                result.exit_code,
            )
        self._coordinator.completed.add(self._dut_id)
        return PASS

    async def _abort_trigger(self, utils: TestUtils) -> Verdict:
        del utils
        return PASS


class _SyntheticRack(LitmusTest):
    """Compose exactly seventy independent synthetic DUT lifecycles."""

    def __init__(
        self,
        *,
        transports: Sequence[MockTransport],
        coordinator: _RackCoordinator,
        install: ConnectorInstaller,
        include_abort_trigger: bool = False,
        deadline_dut: int | None = None,
    ) -> None:
        super().__init__(name="synthetic rack")
        self._duts = tuple(
            _SyntheticDut(
                dut_id=index,
                transport=transport,
                coordinator=coordinator,
                install=install,
                include_abort_trigger=include_abort_trigger and index == 0,
                fail_after=(_PARKED_DUT_BUDGET_SECONDS if index == deadline_dut else None),
            )
            for index, transport in enumerate(transports)
        )

    def build_suite(self) -> LitmusTest:
        """Run all DUT lifecycles under the rack's structured-concurrency owner."""
        return self.group("rack DUTs", self._duts)


def _rack(
    install: ConnectorInstaller,
    *,
    failed_dut: int | None = None,
    park_probes: bool = False,
    include_abort_trigger: bool = False,
    deadline_dut: int | None = None,
) -> tuple[_SyntheticRack, _RackCoordinator, tuple[MockTransport, ...]]:
    transports = tuple(MockTransport() for _ in range(_DUT_COUNT))
    if failed_dut is not None:
        transports[failed_dut].script(
            "probe",
            ExecResult(stdout="", stderr="synthetic fault", exit_code=1),
        )
    if park_probes:
        for transport in transports:
            transport.park("probe")
    if deadline_dut is not None:
        transports[deadline_dut].park("probe")

    coordinator = _RackCoordinator(_DUT_COUNT)
    rack = _SyntheticRack(
        transports=transports,
        coordinator=coordinator,
        install=install,
        include_abort_trigger=include_abort_trigger,
        deadline_dut=deadline_dut,
    )
    return rack, coordinator, transports


def _dut_results(result: RunResult) -> tuple[SummaryTreeNode, ...]:
    rack_group = result.summary_tree.children[0]
    return rack_group.children


async def _run_released_rack(install: ConnectorInstaller) -> None:
    rack, coordinator, _ = _rack(install)
    coordinator.release.set()
    result = await rack.run()
    assert result.state is State.PASS


def _retained_litmus_bytes() -> int:
    snapshot = tracemalloc.take_snapshot().filter_traces(
        (tracemalloc.Filter(True, "*/src/litmus/*"),)
    )
    return sum(stat.size for stat in snapshot.statistics("filename"))


class TestRackScale:
    """Prove the event-loop architecture at the claimed seventy-DUT scale."""

    async def test_seventy_duts_run_concurrently_to_completion(
        self, install_mock_connector: ConnectorInstaller
    ) -> None:
        rack, coordinator, transports = _rack(install_mock_connector)
        running = asyncio.create_task(rack.run())

        await asyncio.wait_for(coordinator.all_started.wait(), _WAIT_LIMIT_SECONDS)
        assert coordinator.started == set(range(_DUT_COUNT))
        assert coordinator.completed == set()

        coordinator.release.set()
        result = await asyncio.wait_for(running, _WAIT_LIMIT_SECONDS)

        assert result.state is State.PASS
        assert coordinator.completed == set(range(_DUT_COUNT))
        assert len(_dut_results(result)) == _DUT_COUNT
        assert all(node.state is State.PASS for node in _dut_results(result))
        assert all(transport.commands == ("probe",) for transport in transports)
        assert all(transport.disconnect_count == 1 for transport in transports)

    async def test_one_dut_failure_leaves_the_other_sixty_nine_unaffected(
        self, install_mock_connector: ConnectorInstaller
    ) -> None:
        failed_dut = 12
        rack, coordinator, transports = _rack(install_mock_connector, failed_dut=failed_dut)
        coordinator.release.set()

        result = await asyncio.wait_for(rack.run(), _WAIT_LIMIT_SECONDS)
        dut_results = _dut_results(result)

        assert result.state is State.FAIL
        assert [node.state for node in dut_results].count(State.FAIL) == 1
        assert dut_results[failed_dut].state is State.FAIL
        assert coordinator.completed == set(range(_DUT_COUNT)) - {failed_dut}
        assert all(transport.commands == ("probe",) for transport in transports)
        assert all(transport.disconnect_count == 1 for transport in transports)

    async def test_retained_framework_memory_stays_flat_across_loaded_racks(
        self, install_mock_connector: ConnectorInstaller
    ) -> None:
        await _run_released_rack(install_mock_connector)
        gc.collect()
        tracemalloc.start()
        try:
            await _run_released_rack(install_mock_connector)
            gc.collect()
            baseline = _retained_litmus_bytes()
            samples: list[int] = []
            for _ in range(_MEMORY_ROUNDS):
                await _run_released_rack(install_mock_connector)
                gc.collect()
                samples.append(_retained_litmus_bytes())
        finally:
            tracemalloc.stop()

        retained_growth = max(samples) - baseline
        assert retained_growth <= _RETAINED_MEMORY_LIMIT_BYTES, (
            f"retained Litmus memory grew by {retained_growth} bytes; "
            f"limit is {_RETAINED_MEMORY_LIMIT_BYTES} bytes"
        )

    async def test_whole_rack_abort_latency_is_below_one_second(
        self, install_mock_connector: ConnectorInstaller
    ) -> None:
        rack, coordinator, transports = _rack(
            install_mock_connector,
            park_probes=True,
            include_abort_trigger=True,
        )
        measurement = _AbortMeasurement()

        async def before_each(ctx: BeforeEachContext) -> None:
            if ctx.name != "trigger whole-rack abort":
                return
            await asyncio.wait_for(coordinator.all_started.wait(), _WAIT_LIMIT_SECONDS)
            deadline = asyncio.get_running_loop().time() + _WAIT_LIMIT_SECONDS
            while sum(transport.in_flight_count for transport in transports) != _DUT_COUNT:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("not every DUT entered its parked transport")
                await asyncio.sleep(0)
            measurement.requested_at = asyncio.get_running_loop().time()
            raise RuntimeError("synthetic rack controller unavailable")

        coordinator.release.set()
        result = await asyncio.wait_for(
            rack.run(before_each=before_each, allow_unpaired_hooks=True),
            _WAIT_LIMIT_SECONDS,
        )
        settled_at = asyncio.get_running_loop().time()

        assert measurement.requested_at is not None
        abort_latency = settled_at - measurement.requested_at
        assert abort_latency < _ABORT_LATENCY_LIMIT_SECONDS, (
            f"whole-rack abort took {abort_latency:.6f}s; "
            f"limit is {_ABORT_LATENCY_LIMIT_SECONDS:.1f}s"
        )
        assert result.state is State.ABORTED
        assert all(transport.cancel_count == 1 for transport in transports)
        assert all(not transport.connected for transport in transports)
        assert all(not transport.has_in_flight for transport in transports)

    async def test_operator_interrupt_hard_closes_seventy_mock_transports(
        self, install_mock_connector: ConnectorInstaller
    ) -> None:
        rack, coordinator, transports = _rack(install_mock_connector, park_probes=True)
        coordinator.release.set()
        running = asyncio.create_task(rack.run())

        await asyncio.wait_for(coordinator.all_started.wait(), _WAIT_LIMIT_SECONDS)
        deadline = asyncio.get_running_loop().time() + _WAIT_LIMIT_SECONDS
        while sum(transport.in_flight_count for transport in transports) != _DUT_COUNT:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("not every DUT entered its parked transport")
            await asyncio.sleep(0)

        os.kill(os.getpid(), signal.SIGINT)
        result = await asyncio.wait_for(running, _WAIT_LIMIT_SECONDS)

        assert result.state is State.ABORTED
        assert isinstance(result.failure, OperatorInterrupt)
        assert "operator" in result.failure.fail_message
        assert all(node.state is State.ABORTED for node in _dut_results(result))
        assert all(isinstance(node.failure, OperatorInterrupt) for node in _dut_results(result))
        assert all(transport.cancel_count == 1 for transport in transports)
        assert all(not transport.connected for transport in transports)
        assert all(not transport.has_in_flight for transport in transports)

    async def test_one_parked_dut_is_cut_within_budget_while_sixty_nine_finish(
        self,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        parked_dut = 12
        rack, coordinator, transports = _rack(
            install_mock_connector,
            deadline_dut=parked_dut,
        )
        coordinator.release.set()
        started_at = asyncio.get_running_loop().time()

        result = await asyncio.wait_for(rack.run(), _WAIT_LIMIT_SECONDS)
        elapsed = asyncio.get_running_loop().time() - started_at
        dut_results = _dut_results(result)

        assert elapsed < _PARKED_DUT_BUDGET_SECONDS + _DEADLINE_LATENCY_LIMIT_SECONDS
        assert result.state is State.FAIL
        assert dut_results[parked_dut].state is State.FAIL
        assert dut_results[parked_dut].timeout is not None
        assert coordinator.completed == set(range(_DUT_COUNT)) - {parked_dut}
        assert transports[parked_dut].cancel_count == 1
        assert not transports[parked_dut].has_in_flight
        assert all(
            transport.disconnect_count == 1
            for index, transport in enumerate(transports)
            if index != parked_dut
        )

    async def test_rack_deadline_keeps_moving_while_one_sibling_is_in_a_hook(
        self,
        install_mock_connector: ConnectorInstaller,
    ) -> None:
        parked_dut = 12
        rack, coordinator, transports = _rack(install_mock_connector)
        transports[parked_dut].park("probe")

        async def slow_sibling_hook(ctx: BeforeEachContext) -> None:
            if ctx.name == "dut-00":
                await asyncio.sleep(0.3)

        coordinator.release.set()
        started_at = asyncio.get_running_loop().time()
        result = await asyncio.wait_for(
            rack.run(
                fail_after=_PARKED_DUT_BUDGET_SECONDS,
                before_each=slow_sibling_hook,
                allow_unpaired_hooks=True,
            ),
            _WAIT_LIMIT_SECONDS,
        )
        elapsed = asyncio.get_running_loop().time() - started_at

        assert elapsed < _PARKED_DUT_BUDGET_SECONDS + 0.15
        assert result.state is State.FAIL
        assert result.timeout is not None
        assert result.timeout.clock == "synthetic rack"
        assert all(node.state is not None for node in _dut_results(result))
        assert transports[parked_dut].cancel_count == 1
