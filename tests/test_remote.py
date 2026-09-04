"""The remote transport contract: connect, exec, cancel_in_flight.

Serial/UART is out of scope for the port, but the protocol must not preclude it.
The UART-shaped stub below is that proof — it implements the three hooks and
nothing SSH-specific.
"""

from __future__ import annotations

import asyncio

import pytest

from litmus.executor import ExecResult
from litmus.remote import RemoteInterface


class _UartStub(RemoteInterface):
    """A serial-shaped transport: connect, exec, close. No SSH surface."""

    def __init__(self) -> None:
        super().__init__()
        self.disconnect_calls = 0
        self._in_flight: asyncio.Future[ExecResult] | None = None

    async def _connect(self) -> None:
        return

    async def _disconnect(self) -> None:
        self.disconnect_calls += 1
        in_flight = self._in_flight
        if in_flight is not None and not in_flight.done():
            in_flight.set_exception(ConnectionError("transport closed"))

    async def _exec(self, command: str) -> ExecResult:
        if command == "block":
            in_flight: asyncio.Future[ExecResult] = asyncio.get_running_loop().create_future()
            self._in_flight = in_flight
            return await in_flight
        if command == "fail":
            return ExecResult(stdout="", stderr="nope", exit_code=1)
        return ExecResult(stdout=command, stderr="", exit_code=0)


class _DisconnectRaises(RemoteInterface):
    """Graceful close fails after the transport is open — the flag must still clear."""

    async def _connect(self) -> None:
        return

    async def _disconnect(self) -> None:
        raise RuntimeError("close failed")

    async def _exec(self, command: str) -> ExecResult:
        return ExecResult(stdout=command, stderr="", exit_code=0)


class _HardClose(RemoteInterface):
    """Overrides the default cancel path the way SSH will: destroy, don't hang up."""

    def __init__(self) -> None:
        super().__init__()
        self.hard_closed = False
        self.disconnected = False

    async def _connect(self) -> None:
        return

    async def _disconnect(self) -> None:
        self.disconnected = True

    async def _exec(self, command: str) -> ExecResult:
        return ExecResult(stdout=command, stderr="", exit_code=0)

    async def _cancel_in_flight(self) -> None:
        self.hard_closed = True


class TestPublicSurface:
    def test_remote_interface_is_not_on_the_public_surface(self) -> None:
        import litmus

        assert not hasattr(litmus, "RemoteInterface")
        assert "RemoteInterface" not in litmus.__all__

    def test_the_protocol_cannot_be_constructed(self) -> None:
        with pytest.raises(TypeError, match="abstract"):
            RemoteInterface()  # type: ignore[abstract]


class TestUartShapedPeer:
    """Serial/UART must be able to sit next to SSH without protocol changes."""

    def test_uart_stub_has_no_ssh_surface(self) -> None:
        transport = _UartStub()

        assert not hasattr(transport, "via")
        assert not hasattr(transport, "forward_out")
        assert not hasattr(transport, "_forward_out")

    async def test_connect_then_exec_returns_evidence(self) -> None:
        transport = _UartStub()

        await transport.connect()

        assert transport.connected is True
        result = await transport.exec("at")
        assert result == ExecResult(stdout="at", stderr="", exit_code=0)

    async def test_a_failed_command_is_evidence_not_an_exception(self) -> None:
        transport = _UartStub()
        await transport.connect()

        result = await transport.exec("fail")

        assert result.exit_code == 1
        assert result.stderr == "nope"

    async def test_connect_is_idempotent(self) -> None:
        transport = _UartStub()
        await transport.connect()
        await transport.connect()

        assert transport.connected is True

    async def test_an_addressless_transport_rejects_a_host(self) -> None:
        transport = _UartStub()

        assert transport.address is None
        with pytest.raises(TypeError, match="has no address"):
            await transport.connect("10.0.0.2")
        with pytest.raises(TypeError, match="has no address"):
            await transport.connect(port=22)

        assert transport.connected is False

    async def test_disconnect_is_safe_when_never_connected(self) -> None:
        transport = _UartStub()

        await transport.disconnect()

        assert transport.connected is False
        assert transport.disconnect_calls == 1

    async def test_reconnect_replaces_the_underlying_connection(self) -> None:
        transport = _UartStub()
        await transport.connect()

        await transport.reconnect()

        assert transport.connected is True
        assert transport.disconnect_calls == 1
        assert (await transport.exec("at")).exit_code == 0


class TestDisconnectClearsTheFlag:
    async def test_connected_is_false_and_later_exec_rejects_when_close_raises(self) -> None:
        transport = _DisconnectRaises()
        await transport.connect()

        with pytest.raises(RuntimeError, match="close failed"):
            await transport.disconnect()

        assert transport.connected is False
        with pytest.raises(ConnectionError, match="not connected"):
            await transport.exec("at")


class TestExecRejectsWhenClosed:
    async def test_exec_before_connect_rejects(self) -> None:
        transport = _UartStub()

        with pytest.raises(ConnectionError, match="not connected"):
            await transport.exec("at")

    async def test_exec_after_disconnect_rejects(self) -> None:
        transport = _UartStub()
        await transport.connect()
        await transport.disconnect()

        with pytest.raises(ConnectionError, match="not connected"):
            await transport.exec("at")
        assert transport.connected is False


class TestCancelInFlight:
    async def test_is_safe_when_nothing_is_running(self) -> None:
        transport = _UartStub()
        await transport.connect()

        await transport.cancel_in_flight()
        await transport.cancel_in_flight()

        assert transport.connected is False

    async def test_is_safe_when_never_connected(self) -> None:
        await _UartStub().cancel_in_flight()

    async def test_leaves_connected_false_and_further_exec_rejects(self) -> None:
        transport = _UartStub()
        await transport.connect()

        await transport.cancel_in_flight()

        assert transport.connected is False
        with pytest.raises(ConnectionError, match="not connected"):
            await transport.exec("at")

    async def test_in_flight_exec_fails_at_the_protocol_level(self) -> None:
        transport = _UartStub()
        await transport.connect()
        task = asyncio.create_task(transport.exec("block"))
        deadline = asyncio.get_running_loop().time() + 2
        while transport._in_flight is None:  # pyright: ignore[reportPrivateUsage]
            assert asyncio.get_running_loop().time() < deadline, "exec never started"
            await asyncio.sleep(0)

        await transport.cancel_in_flight()

        with pytest.raises(ConnectionError, match="transport closed"):
            await task
        assert transport.connected is False
        with pytest.raises(ConnectionError, match="not connected"):
            await transport.exec("at")

    async def test_default_cancel_delegates_to_disconnect(self) -> None:
        transport = _UartStub()
        await transport.connect()

        await transport.cancel_in_flight()

        assert transport.disconnect_calls == 1

    async def test_subclass_may_override_for_a_hard_close(self) -> None:
        transport = _HardClose()
        await transport.connect()

        await transport.cancel_in_flight()

        assert transport.hard_closed is True
        assert transport.disconnected is False
        assert transport.connected is False


class _Hopped(RemoteInterface):
    """A transport that dials through an upstream one, the way a tunnel does."""

    def __init__(self, host: str, hop: RemoteInterface | None = None) -> None:
        super().__init__()
        self.host = host
        self._hop = hop

    @property
    def hop(self) -> RemoteInterface | None:
        return self._hop

    async def _connect(self) -> None:
        return

    async def _disconnect(self) -> None:
        return

    async def _exec(self, command: str) -> ExecResult:
        return ExecResult(stdout=command, stderr="", exit_code=0)


class TestTopologySeams:
    """`endpoint`, `hop`, and `route_label` are what provenance lines are built from."""

    def test_a_transport_with_no_address_is_still_named(self) -> None:
        transport = _UartStub()

        assert transport.endpoint == "remote"
        assert transport.hop is None
        assert transport.route_label == "remote"

    def test_a_host_bearing_transport_names_its_peer(self) -> None:
        transport = _Hopped("dut")

        assert transport.endpoint == "dut"
        assert transport.route_label == "dut"

    def test_the_chain_renders_test_host_first_and_in_full(self) -> None:
        bastion = _Hopped("bastion")
        edge = _Hopped("edge", bastion)
        dut = _Hopped("dut", edge)

        assert dut.route_label == "bastion -> edge -> dut"
        assert edge.route_label == "bastion -> edge"

    def test_the_default_connect_line_states_the_route(self) -> None:
        dut = _Hopped("dut", _Hopped("bastion"))

        assert dut.connect_provenance == "connect: bastion -> dut"
