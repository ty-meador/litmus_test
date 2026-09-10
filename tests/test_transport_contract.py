"""Shared RemoteInterface contract, parametrized over every transport.

MockTransport, the loopback SshInterface, and SerialInterface all sit this bar.
A new transport lands against the same assertions by adding one fixture and
listing it in TRANSPORTS.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import final

import pytest
from tests.fake_console import open_pty_console
from tests.loopback_sshd import LoopbackSshd

from litmus.executor import ExecResult
from litmus.mock import MockTransport
from litmus.remote import RemoteInterface
from litmus.serial import SerialInterface
from litmus.ssh import SshInterface

type InFlightWaiter = Callable[[RemoteInterface], Awaitable[None]]


@final
class TransportPeer:
    """A disconnected transport plus the commands this peer understands.

    The fixture owns the backing (scripted mock, loopback sshd). Tests talk to
    `.transport` and the command names; adding a transport is adding a fixture
    that yields one of these.
    """

    def __init__(
        self,
        transport: RemoteInterface,
        *,
        success: str,
        fail: str,
        block: str,
        fail_stderr: str,
        wait_in_flight: InFlightWaiter,
    ) -> None:
        self.transport = transport
        self.success = success
        self.fail = fail
        self.block = block
        self.fail_stderr = fail_stderr
        self._wait_in_flight = wait_in_flight

    async def start_blocked_exec(self) -> asyncio.Task[ExecResult]:
        """Start `block` and wait until it is actually in flight on this transport."""
        task = asyncio.create_task(self.transport.exec(self.block))
        await self._wait_in_flight(self.transport)
        return task


async def _wait_mock_in_flight(transport: RemoteInterface) -> None:
    assert isinstance(transport, MockTransport)
    deadline = asyncio.get_running_loop().time() + 2
    while not transport.has_in_flight:
        assert asyncio.get_running_loop().time() < deadline, "parked exec never started"
        await asyncio.sleep(0)


async def _wait_ssh_in_flight(_transport: RemoteInterface) -> None:
    await asyncio.sleep(0.2)


async def _wait_serial_in_flight(_transport: RemoteInterface) -> None:
    await asyncio.sleep(0.2)


@pytest.fixture
async def mock_peer() -> AsyncIterator[TransportPeer]:
    """In-process MockTransport. No sshd. `block` is parked until cancel."""
    transport = MockTransport()
    transport.script("fail", ExecResult(stdout="", stderr="nope", exit_code=1))
    transport.park("sleep 30")
    peer = TransportPeer(
        transport,
        success="uname",
        fail="fail",
        block="sleep 30",
        fail_stderr="nope",
        wait_in_flight=_wait_mock_in_flight,
    )
    try:
        yield peer
    finally:
        await transport.disconnect()


@pytest.fixture
async def ssh_peer() -> AsyncIterator[TransportPeer]:
    """Loopback SshInterface. `block` is a remote sleep the hard-close must kill."""
    listener = await LoopbackSshd.start()
    password = listener.password
    assert password is not None
    transport = SshInterface(
        host="127.0.0.1",
        port=listener.port,
        username=listener.username,
        password=password,
        skip_host_verification=True,
    )
    peer = TransportPeer(
        transport,
        success="uname",
        fail="fail",
        block="sleep 30",
        fail_stderr="nope",
        wait_in_flight=_wait_ssh_in_flight,
    )
    try:
        yield peer
    finally:
        await transport.disconnect()
        await listener.aclose()


@pytest.fixture
async def serial_peer() -> AsyncIterator[TransportPeer]:
    """Pty-backed SerialInterface. `block` parks until the hard-close kills it."""
    async with open_pty_console() as (device, _console):
        transport = SerialInterface(device=device, baud=115200)
        peer = TransportPeer(
            transport,
            success="uname",
            fail="fail",
            block="sleep 30",
            fail_stderr="",
            wait_in_flight=_wait_serial_in_flight,
        )
        try:
            yield peer
        finally:
            await transport.disconnect()


# Adding a transport: add one fixture above, then name it here.
TRANSPORTS = ("mock_peer", "ssh_peer", "serial_peer")


@pytest.fixture(params=TRANSPORTS)
def peer(request: pytest.FixtureRequest) -> TransportPeer:
    """Every contract test runs once per transport fixture in TRANSPORTS."""
    value = request.getfixturevalue(request.param)
    assert isinstance(value, TransportPeer)
    return value


async def _assert_named_hard_close(task: asyncio.Task[ExecResult]) -> None:
    """In-flight exec failed because the resource died, named `transport closed`.

    Mock raises `ConnectionError`. SSH and serial absorb the drop into
    `ExecResult` with `exit_code == -1`. The cause name is the contract, not
    the settlement shape.
    """
    try:
        result: ExecResult | BaseException = await task
    except ConnectionError as error:
        result = error
    if isinstance(result, ConnectionError):
        assert "transport closed" in str(result)
        return
    assert not isinstance(result, BaseException)
    assert result.exit_code == -1
    assert "transport closed" in result.stderr


class TestConnectDisconnectIdempotence:
    async def test_connect_twice_stays_connected(self, peer: TransportPeer) -> None:
        await peer.transport.connect()
        await peer.transport.connect()

        assert peer.transport.connected is True
        result = await peer.transport.exec(peer.success)
        assert result.exit_code == 0

    async def test_disconnect_is_safe_when_never_connected(self, peer: TransportPeer) -> None:
        await peer.transport.disconnect()

        assert peer.transport.connected is False

    async def test_disconnect_twice_stays_closed(self, peer: TransportPeer) -> None:
        await peer.transport.connect()
        await peer.transport.disconnect()
        await peer.transport.disconnect()

        assert peer.transport.connected is False


class TestExecNeverThrows:
    async def test_a_successful_command_is_evidence(self, peer: TransportPeer) -> None:
        await peer.transport.connect()

        result = await peer.transport.exec(peer.success)

        assert result == ExecResult(stdout=peer.success, stderr="", exit_code=0)

    async def test_a_failed_command_is_evidence_not_an_exception(self, peer: TransportPeer) -> None:
        await peer.transport.connect()

        result = await peer.transport.exec(peer.fail)

        assert result.exit_code == 1
        assert result.stderr == peer.fail_stderr


class TestCancelInFlight:
    async def test_is_safe_when_never_connected(self, peer: TransportPeer) -> None:
        await peer.transport.cancel_in_flight()

        assert peer.transport.connected is False

    async def test_is_idempotent_when_idle(self, peer: TransportPeer) -> None:
        await peer.transport.connect()

        await peer.transport.cancel_in_flight()
        await peer.transport.cancel_in_flight()

        assert peer.transport.connected is False

    async def test_leaves_connected_false_and_further_exec_rejects(
        self, peer: TransportPeer
    ) -> None:
        await peer.transport.connect()

        await peer.transport.cancel_in_flight()

        assert peer.transport.connected is False
        with pytest.raises(ConnectionError, match="not connected"):
            await peer.transport.exec(peer.success)

    async def test_in_flight_exec_fails_because_the_resource_is_dead(
        self, peer: TransportPeer
    ) -> None:
        await peer.transport.connect()
        task = await peer.start_blocked_exec()

        await peer.transport.cancel_in_flight()

        await _assert_named_hard_close(task)
        assert peer.transport.connected is False
        with pytest.raises(ConnectionError, match="not connected"):
            await peer.transport.exec(peer.success)


class TestHardCloseCause:
    async def test_the_dead_resource_is_named_transport_closed(self, peer: TransportPeer) -> None:
        await peer.transport.connect()
        task = await peer.start_blocked_exec()

        await peer.transport.cancel_in_flight()

        await _assert_named_hard_close(task)


class TestConnectedFlagOwnership:
    async def test_exec_before_connect_rejects(self, peer: TransportPeer) -> None:
        with pytest.raises(ConnectionError, match="not connected"):
            await peer.transport.exec(peer.success)
        assert peer.transport.connected is False

    async def test_disconnect_clears_the_flag_and_later_exec_rejects(
        self, peer: TransportPeer
    ) -> None:
        await peer.transport.connect()
        await peer.transport.disconnect()

        assert peer.transport.connected is False
        with pytest.raises(ConnectionError, match="not connected"):
            await peer.transport.exec(peer.success)
