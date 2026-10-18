"""Issue #61: operator SIGINT/SIGTERM is a run-abort, not an unrecorded unwind."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

import asyncssh
import pytest
from tests.loopback_sshd import LoopbackSshd
from tests.mock_connector import ConnectorInstaller

from litmus import (
    PASS,
    ExternalIntegrationFailure,
    LitmusTest,
    OperatorInterrupt,
    RunResult,
    TestUtils,
    Verdict,
)
from litmus.lifecycle import RunAbort, State
from litmus.mock import MockTransport

_WAIT_LIMIT_SECONDS = 5.0
_SSH_DUT_COUNT = 70
_SSH_WAIT_LIMIT_SECONDS = 15.0


def _loop_signal_handlers() -> dict[int, object]:
    """Snapshot the running loop's signal table. Empty when the loop has none."""
    loop = asyncio.get_running_loop()
    handlers = getattr(loop, "_signal_handlers", None)
    return dict(handlers) if handlers is not None else {}


@contextmanager
def _capture_process_exits(monkeypatch: pytest.MonkeyPatch) -> Generator[list[int]]:
    """Replace ``os._exit`` so the second signal cannot kill pytest."""
    seen: list[int] = []

    def record(code: int) -> None:
        seen.append(int(code))

    monkeypatch.setattr("litmus.base.os._exit", record)
    yield seen


class _ParkedSuite(LitmusTest):
    """One leaf that waits until the operator (or the test) releases it."""

    def __init__(
        self,
        *,
        name: str = "rack",
        started: asyncio.Event | None = None,
        in_cleanup: asyncio.Event | None = None,
        release_cleanup: asyncio.Event | None = None,
    ) -> None:
        super().__init__(name=name)
        self.started = asyncio.Event() if started is None else started
        self.in_cleanup = asyncio.Event() if in_cleanup is None else in_cleanup
        self.release_cleanup = asyncio.Event() if release_cleanup is None else release_cleanup

    def build_suite(self) -> LitmusTest:
        return self.test("probe", self._probe)

    async def _probe(self, utils: TestUtils) -> Verdict:
        del utils
        self.started.set()
        await asyncio.Event().wait()
        return PASS

    async def cleanup(self, utils: TestUtils) -> None:
        del utils
        self.in_cleanup.set()
        await self.release_cleanup.wait()


class _PassingSuite(LitmusTest):
    def build_suite(self) -> LitmusTest:
        return self.test("ok", self._ok)

    async def _ok(self, utils: TestUtils) -> Verdict:
        del utils
        return PASS


class _GroupedParkedSuite(LitmusTest):
    def __init__(self, *, parked: asyncio.Event, halt_started: asyncio.Event) -> None:
        super().__init__(name="rack")
        self._parked = parked
        self._halt_started = halt_started

    def build_suite(self) -> LitmusTest:
        return self.parallel(
            "parallel",
            [
                self.test("slow", self._slow),
                self.test("halt", self._halt),
            ],
        )

    async def _slow(self, utils: TestUtils) -> Verdict:
        del utils
        self._parked.set()
        await asyncio.Event().wait()
        return PASS

    async def _halt(self, utils: TestUtils) -> Verdict:
        del utils
        self._halt_started.set()
        await asyncio.Event().wait()
        return PASS


def _summary_states(result: RunResult) -> dict[str, State | None]:
    states: dict[str, State | None] = {}

    def walk(node: Any) -> None:
        states[node.name] = node.state
        for child in node.children:
            walk(child)

    walk(result.summary_tree)
    return states


class TestOperatorInterruptTaxonomyOnALiveRun:
    """AC: SIGINT/SIGTERM produce Aborted everywhere with an operator-named failure."""

    @pytest.mark.parametrize(
        "signum",
        [signal.SIGINT, signal.SIGTERM],
        ids=["SIGINT", "SIGTERM"],
    )
    async def test_operator_signal_aborts_the_run_and_returns_a_result(
        self, signum: signal.Signals
    ) -> None:
        handlers_before = _loop_signal_handlers()
        suite = _ParkedSuite()
        suite.release_cleanup.set()
        running = asyncio.create_task(suite.run())
        await asyncio.wait_for(suite.started.wait(), _WAIT_LIMIT_SECONDS)

        os.kill(os.getpid(), signum)
        result = await asyncio.wait_for(running, _WAIT_LIMIT_SECONDS)

        assert result.state is State.ABORTED
        assert isinstance(result.failure, OperatorInterrupt)
        assert result.failure is not None
        assert result.failure.signum == int(signum)
        assert result.failure.signal_name == signum.name
        assert "operator" in result.failure.fail_message
        assert result.failure.signal_name in result.failure.fail_message
        assert _summary_states(result)["rack"] is State.ABORTED
        assert _summary_states(result)["probe"] is State.ABORTED
        assert _loop_signal_handlers() == handlers_before

    async def test_every_node_in_a_parallel_node_is_aborted_with_the_same_operator_failure(
        self,
    ) -> None:
        parked = asyncio.Event()
        halt_started = asyncio.Event()
        suite = _GroupedParkedSuite(parked=parked, halt_started=halt_started)
        running = asyncio.create_task(suite.run())
        await asyncio.wait_for(parked.wait(), _WAIT_LIMIT_SECONDS)
        await asyncio.wait_for(halt_started.wait(), _WAIT_LIMIT_SECONDS)

        os.kill(os.getpid(), signal.SIGINT)
        result = await asyncio.wait_for(running, _WAIT_LIMIT_SECONDS)

        assert result.state is State.ABORTED
        assert isinstance(result.failure, OperatorInterrupt)
        assert _summary_states(result) == {
            "rack": State.ABORTED,
            "parallel": State.ABORTED,
            "slow": State.ABORTED,
            "halt": State.ABORTED,
        }
        assert result.summary_tree.failure is result.failure


class TestHandlerScope:
    """AC: handler is scoped to the live run; nothing remains after run() returns."""

    async def test_a_completed_run_does_not_leave_a_handler(self) -> None:
        handlers_before = _loop_signal_handlers()
        result = await _PassingSuite(name="ok").run()
        assert result.state is State.PASS
        assert _loop_signal_handlers() == handlers_before

    async def test_a_second_run_installs_and_removes_again(self) -> None:
        handlers_before = _loop_signal_handlers()
        first = await _PassingSuite(name="first").run()
        second = await _PassingSuite(name="second").run()
        assert first.state is State.PASS
        assert second.state is State.PASS
        assert _loop_signal_handlers() == handlers_before

    async def test_a_raising_run_still_drops_the_handler(self) -> None:
        handlers_before = _loop_signal_handlers()

        class Boom(LitmusTest):
            def build_suite(self) -> LitmusTest:
                return self.test("boom", self._boom)

            async def _boom(self, utils: TestUtils) -> Verdict:
                del utils
                raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            await Boom(name="boom").run()
        assert _loop_signal_handlers() == handlers_before

    async def test_a_failed_install_still_drops_partial_handlers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handlers_before = _loop_signal_handlers()
        loop = asyncio.get_running_loop()
        original = loop.add_signal_handler

        def flaky(sig: int, callback: Callable[..., None], *args: object) -> None:
            if sig == signal.SIGTERM:
                raise RuntimeError("no SIGTERM on this loop")
            original(sig, callback, *args)

        monkeypatch.setattr(loop, "add_signal_handler", flaky)
        with pytest.raises(RuntimeError, match="no SIGTERM"):
            await _PassingSuite(name="ok").run()
        assert _loop_signal_handlers() == handlers_before


class TestSecondSignalEscalates:
    """AC: second signal inside the finalization window exits immediately."""

    async def test_second_signal_during_cleanup_escalates_without_killing_pytest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _capture_process_exits(monkeypatch) as exits:
            suite = _ParkedSuite()
            running = asyncio.create_task(suite.run())
            await asyncio.wait_for(suite.started.wait(), _WAIT_LIMIT_SECONDS)

            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.wait_for(suite.in_cleanup.wait(), _WAIT_LIMIT_SECONDS)
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.sleep(0)

            suite.release_cleanup.set()
            result = await asyncio.wait_for(running, _WAIT_LIMIT_SECONDS)

        assert exits == [128 + int(signal.SIGINT)]
        assert result.state is State.ABORTED
        assert isinstance(result.failure, OperatorInterrupt)


class TestOperatorAbortHardClosesTransports:
    """AC: every registered transport is hard-closed; nothing left to GC."""

    async def test_mock_transport_is_hard_closed_before_run_returns(
        self, install_mock_connector: ConnectorInstaller
    ) -> None:
        transport = MockTransport()
        transport.park("flash")
        in_flight = asyncio.Event()

        class FlashSuite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                install_mock_connector(utils, transport)
                await utils.use_shell(host="dut", username="root")
                return {}

            def build_suite(self) -> LitmusTest:
                return self.test("flash", self._flash)

            async def _flash(self, utils: TestUtils) -> Verdict:
                in_flight.set()
                await utils.exec("flash")
                return PASS

        running = asyncio.create_task(FlashSuite(name="rack").run())
        await asyncio.wait_for(in_flight.wait(), _WAIT_LIMIT_SECONDS)
        deadline = asyncio.get_running_loop().time() + _WAIT_LIMIT_SECONDS
        while not transport.has_in_flight:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("parked flash never entered the transport")
            await asyncio.sleep(0)

        os.kill(os.getpid(), signal.SIGINT)
        result = await asyncio.wait_for(running, _WAIT_LIMIT_SECONDS)

        assert result.state is State.ABORTED
        assert isinstance(result.failure, OperatorInterrupt)
        assert transport.cancel_count == 1
        assert transport.connected is False
        assert transport.has_in_flight is False


class _SshDut(LitmusTest):
    """One DUT dialing the in-process sshd and parking a remote sleep."""

    def __init__(
        self,
        *,
        dut_id: int,
        peer: LoopbackSshd,
        coordinator: _SshCoordinator,
    ) -> None:
        super().__init__(name=f"dut-{dut_id:02d}")
        self._dut_id = dut_id
        self._peer = peer
        self._coordinator = coordinator

    async def configure(self, utils: TestUtils) -> dict[str, object]:
        password = self._peer.password
        assert password is not None
        await utils.use_shell(
            host="127.0.0.1",
            port=self._peer.port,
            username=self._peer.username,
            password=password,
            skip_host_verification=True,
        )
        return {}

    def build_suite(self) -> LitmusTest:
        return self.test("flash", self._flash)

    async def _flash(self, utils: TestUtils) -> Verdict:
        await self._coordinator.rendezvous(self._dut_id)
        self._coordinator.mark_executing(self._dut_id)
        await utils.exec("sleep 30")
        return PASS


class _SshCoordinator:
    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.started: set[int] = set()
        self.executing: set[int] = set()
        self.all_started = asyncio.Event()
        self.all_executing = asyncio.Event()
        self.release = asyncio.Event()

    async def rendezvous(self, dut_id: int) -> None:
        self.started.add(dut_id)
        if len(self.started) == self.expected:
            self.all_started.set()
        await self.release.wait()

    def mark_executing(self, dut_id: int) -> None:
        self.executing.add(dut_id)
        if len(self.executing) == self.expected:
            self.all_executing.set()


class _SshRack(LitmusTest):
    def __init__(self, *, peer: LoopbackSshd, coordinator: _SshCoordinator) -> None:
        super().__init__(name="ssh rack")
        self._duts = tuple(
            _SshDut(dut_id=index, peer=peer, coordinator=coordinator)
            for index in range(_SSH_DUT_COUNT)
        )

    def build_suite(self) -> LitmusTest:
        return self.parallel("rack DUTs", self._duts)


class _AbortCounter:
    """Count ``SSHClientConnection.abort`` while still performing the real abort."""

    def __init__(self) -> None:
        self.calls = 0
        self._original = asyncssh.SSHClientConnection.abort

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        counter = self
        original = self._original

        def tracked(connection: asyncssh.SSHClientConnection) -> None:
            counter.calls += 1
            original(connection)

        monkeypatch.setattr(asyncssh.SSHClientConnection, "abort", tracked)


class TestOperatorAbortOnLoopbackSshd:
    """AC: seventy DUTs on the loopback sshd are hard-closed at the socket."""

    async def test_seventy_sshd_duts_are_hard_closed_on_sigint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        aborts = _AbortCounter()
        aborts.install(monkeypatch)
        peer = await LoopbackSshd.start()
        coordinator = _SshCoordinator(_SSH_DUT_COUNT)
        rack = _SshRack(peer=peer, coordinator=coordinator)
        running = asyncio.create_task(rack.run())
        try:
            await asyncio.wait_for(coordinator.all_started.wait(), _SSH_WAIT_LIMIT_SECONDS)
            assert coordinator.started == set(range(_SSH_DUT_COUNT))
            coordinator.release.set()
            await asyncio.wait_for(coordinator.all_executing.wait(), _SSH_WAIT_LIMIT_SECONDS)
            await asyncio.sleep(0.2)

            os.kill(os.getpid(), signal.SIGINT)
            result = await asyncio.wait_for(running, _SSH_WAIT_LIMIT_SECONDS)

            assert result.state is State.ABORTED
            assert isinstance(result.failure, OperatorInterrupt)
            assert "operator" in result.failure.fail_message
            assert aborts.calls >= _SSH_DUT_COUNT
            dut_nodes = result.summary_tree.children[0].children
            assert len(dut_nodes) == _SSH_DUT_COUNT
            assert all(node.state is State.ABORTED for node in dut_nodes)
            assert all(isinstance(node.failure, OperatorInterrupt) for node in dut_nodes)
        finally:
            # A failed rendezvous must report, not park 70 DUTs forever.
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            await peer.aclose()


class TestOperatorHaltDoesNotRewriteAHookAbort:
    async def test_the_first_halt_reason_still_wins(self) -> None:
        abort = RunAbort()
        hook = ExternalIntegrationFailure("before_each", RuntimeError("db down"))
        operator = OperatorInterrupt(int(signal.SIGINT))
        abort.fire(hook)
        abort.fire(operator)
        assert abort.reason is hook
