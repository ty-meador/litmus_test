"""MockTransport: a RemoteInterface so lifecycle tests never need real SSH."""

from __future__ import annotations

import asyncio

import pytest

from litmus.executor import ExecResult
from litmus.mock import MockTransport
from litmus.remote import RemoteInterface


class TestPublicSurface:
    def test_mock_transport_is_not_on_the_public_surface(self) -> None:
        import litmus

        assert not hasattr(litmus, "MockTransport")
        assert "MockTransport" not in litmus.__all__

    def test_is_a_remote_interface(self) -> None:
        transport = MockTransport()

        assert isinstance(transport, RemoteInterface)


class TestLifecycleWithoutSsh:
    """The Phase 3 tooling note: connect / exec / cleanup with no peer."""

    async def test_configure_exec_cleanup_never_opens_ssh(self) -> None:
        transport = MockTransport()
        transport.script("uname", ExecResult(stdout="Linux\n", stderr="", exit_code=0))

        await transport.connect()
        result = await transport.exec("uname")
        await transport.disconnect()

        assert result == ExecResult(stdout="Linux\n", stderr="", exit_code=0)
        assert transport.connected is False
        assert transport.connect_count == 1
        assert transport.disconnect_count == 1
        assert transport.commands == ("uname",)

    async def test_abort_during_main_hard_closes_and_rejects_later_exec(self) -> None:
        transport = MockTransport()
        transport.park("flash")

        await transport.connect()
        task = asyncio.create_task(transport.exec("flash"))
        deadline = asyncio.get_running_loop().time() + 2
        while not transport.has_in_flight:
            assert asyncio.get_running_loop().time() < deadline, "parked exec never started"
            await asyncio.sleep(0)

        await transport.cancel_in_flight()

        with pytest.raises(ConnectionError, match="transport closed"):
            await task
        assert transport.connected is False
        assert transport.cancel_count == 1
        with pytest.raises(ConnectionError, match="not connected"):
            await transport.exec("uname")


class TestScriptedEvidence:
    async def test_unscripted_command_echoes_as_stdout(self) -> None:
        transport = MockTransport()
        await transport.connect()

        result = await transport.exec("echo")

        assert result == ExecResult(stdout="echo", stderr="", exit_code=0)

    async def test_a_failed_command_is_evidence_not_an_exception(self) -> None:
        transport = MockTransport()
        transport.script("fail", ExecResult(stdout="", stderr="nope", exit_code=1))
        await transport.connect()

        result = await transport.exec("fail")

        assert result.exit_code == 1
        assert result.stderr == "nope"

    async def test_commands_records_every_exec(self) -> None:
        transport = MockTransport()
        await transport.connect()

        await transport.exec("one")
        await transport.exec("two")

        assert transport.commands == ("one", "two")


class TestCancelAssertions:
    async def test_cancel_is_safe_when_idle(self) -> None:
        transport = MockTransport()
        await transport.connect()

        await transport.cancel_in_flight()
        await transport.cancel_in_flight()

        assert transport.connected is False
        assert transport.cancel_count == 1

    async def test_cancel_is_safe_when_never_connected(self) -> None:
        transport = MockTransport()

        await transport.cancel_in_flight()

        assert transport.cancel_count == 0
        assert transport.connected is False

    async def test_every_parked_exec_fails_when_the_transport_dies(self) -> None:
        transport = MockTransport()
        transport.park("a")
        transport.park("b")
        await transport.connect()
        left = asyncio.create_task(transport.exec("a"))
        right = asyncio.create_task(transport.exec("b"))
        deadline = asyncio.get_running_loop().time() + 2
        while transport.in_flight_count < 2:
            assert asyncio.get_running_loop().time() < deadline, "parked execs never started"
            await asyncio.sleep(0)

        await transport.cancel_in_flight()

        with pytest.raises(ConnectionError, match="transport closed"):
            await left
        with pytest.raises(ConnectionError, match="transport closed"):
            await right
        assert transport.in_flight_count == 0


class TestConnectFailure:
    async def test_fail_next_connect_raises_and_stays_disconnected(self) -> None:
        transport = MockTransport()
        transport.fail_next_connect(ConnectionError("peer unreachable"))

        with pytest.raises(ConnectionError, match="peer unreachable"):
            await transport.connect()

        assert transport.connected is False
        assert transport.connect_count == 1
        await transport.connect()
        assert transport.connected is True
