"""SerialInterface: UART console transport with prompt, sentinel, and via hops."""

from __future__ import annotations

import ast
import asyncio
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import asyncssh
import pytest
from tests.fake_console import SERIAL_BRIDGE_COMMAND, FakeConsole, open_pty_console
from tests.loopback_sshd import LoopbackSshd

from litmus.executor import ExecResult
from litmus.remote import RemoteInterface
from litmus.serial import SerialInterface
from litmus.ssh import SshInterface

ROOT = Path(__file__).resolve().parents[1]


class TestPublicSurface:
    def test_serial_interface_is_not_on_the_public_surface(self) -> None:
        import litmus

        assert not hasattr(litmus, "SerialInterface")
        assert "SerialInterface" not in litmus.__all__

    def test_package_init_does_not_import_serial(self) -> None:
        tree = ast.parse((ROOT / "src" / "litmus" / "__init__.py").read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        assert not any("serial" in name.split(".") for name in imported)

    def test_core_install_does_not_depend_on_pyserial(self) -> None:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        core = data["project"]["dependencies"]
        extra = data["project"]["optional-dependencies"]["serial"]

        assert not any("pyserial" in item for item in core)
        assert any("pyserial-asyncio" in item for item in extra)

    def test_is_a_remote_interface(self) -> None:
        transport = SerialInterface(device="/dev/ttyUSB0")

        assert isinstance(transport, RemoteInterface)
        assert transport.address is None
        assert transport.endpoint == "ttyUSB0"


class TestConstruction:
    def test_empty_device_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="device"):
            SerialInterface(device="")

    def test_baud_must_be_a_positive_int(self) -> None:
        with pytest.raises(TypeError, match="baud"):
            SerialInterface(device="/dev/ttyUSB0", baud=True)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="baud"):
            SerialInterface(device="/dev/ttyUSB0", baud=0)

    def test_timeouts_must_be_positive_and_finite(self) -> None:
        with pytest.raises(TypeError, match="login_timeout"):
            SerialInterface(device="/dev/ttyUSB0", login_timeout=True)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="prompt_timeout"):
            SerialInterface(device="/dev/ttyUSB0", prompt_timeout=0)
        with pytest.raises(ValueError, match="login_timeout"):
            SerialInterface(device="/dev/ttyUSB0", login_timeout=float("nan"))

    def test_username_without_login_prompt_is_kept(self) -> None:
        transport = SerialInterface(device="/dev/ttyUSB0", username="root")

        assert transport.device == "/dev/ttyUSB0"
        assert transport.baud == 115200
        assert transport.output_budget > 0
        assert transport.prompt_timeout == 30.0

    def test_empty_bridge_command_is_rejected(self) -> None:
        hop = SshInterface(host="127.0.0.1", username="ops", password="x")
        with pytest.raises(ValueError, match="bridge_command"):
            SerialInterface(device="/dev/ttyUSB0", via=hop, bridge_command="")

    async def test_host_and_port_are_rejected_on_connect(self) -> None:
        transport = SerialInterface(device="/dev/ttyUSB0")

        with pytest.raises(TypeError, match="no address"):
            await transport.connect("127.0.0.1", 22)

    def test_via_must_be_an_ssh_interface(self) -> None:
        with pytest.raises(TypeError, match="SshInterface"):
            SerialInterface(device="/dev/ttyUSB0", via="not-an-ssh")  # type: ignore[arg-type]

    def test_login_prompt_requires_a_username(self) -> None:
        with pytest.raises(TypeError, match="username"):
            SerialInterface(device="/dev/ttyUSB0", login_prompt=r"login: ")

    def test_provenance_does_not_render_a_password(self) -> None:
        transport = SerialInterface(
            device="/dev/ttyUSB0",
            login_prompt=r"login: ",
            password_prompt=r"Password: ",
            username="root",
            password="secret-token",
        )

        line = transport.connect_provenance
        assert "secret-token" not in line
        assert "via=direct" in line
        assert "/dev/ttyUSB0" in line


class TestLocalExec:
    async def test_command_output_is_evidence(self) -> None:
        async with open_pty_console() as (device, _console):
            transport = SerialInterface(device=device, baud=115200)
            try:
                await transport.connect()
                result = await transport.exec("uname")
                assert result == ExecResult(stdout="uname", stderr="", exit_code=0)
                assert transport.connected is True
            finally:
                await transport.disconnect()

    async def test_a_failed_command_puts_dut_text_on_stdout(self) -> None:
        async with open_pty_console() as (device, _console):
            transport = SerialInterface(device=device)
            try:
                await transport.connect()
                result = await transport.exec("fail")
                assert result.exit_code == 1
                assert result.stdout == "nope"
                assert result.stderr == ""
            finally:
                await transport.disconnect()

    async def test_connect_twice_is_idempotent(self) -> None:
        async with open_pty_console() as (device, _console):
            transport = SerialInterface(device=device)
            try:
                await transport.connect()
                await transport.connect()
                result = await transport.exec("uname")
                assert result.exit_code == 0
            finally:
                await transport.disconnect()

    async def test_disconnect_is_safe_when_never_connected(self) -> None:
        transport = SerialInterface(device="/dev/ttyUSB0")
        await transport.disconnect()
        assert transport.connected is False


class TestLineDiscipline:
    async def test_a_colorized_prompt_still_matches(self) -> None:
        async with open_pty_console(colorize_prompt=True) as (device, _console):
            transport = SerialInterface(device=device)
            try:
                await transport.connect()
                result = await transport.exec("uname")
                assert result == ExecResult(stdout="uname", stderr="", exit_code=0)
            finally:
                await transport.disconnect()

    async def test_echo_already_off_is_unaffected(self) -> None:
        async with open_pty_console(echo=False) as (device, _console):
            transport = SerialInterface(device=device)
            try:
                await transport.connect()
                result = await transport.exec("uname")
                assert result == ExecResult(stdout="uname", stderr="", exit_code=0)
            finally:
                await transport.disconnect()

    async def test_a_lost_sentinel_settles_minus_one_and_keeps_the_text(self) -> None:
        async with open_pty_console(emit_sentinel=False) as (device, _console):
            transport = SerialInterface(device=device)
            try:
                await transport.connect()
                result = await transport.exec("uname")
                assert result.exit_code == -1
                assert "uname" in result.stdout
            finally:
                await transport.disconnect()


class TestLogin:
    async def test_login_sequence_reaches_the_shell(self) -> None:
        async with open_pty_console(
            login_prompt="login: ",
            password_prompt="Password: ",
        ) as (device, _console):
            transport = SerialInterface(
                device=device,
                login_prompt=r"login: ",
                password_prompt=r"Password: ",
                username="root",
                password="admin",
            )
            try:
                await transport.connect()
                result = await transport.exec("uname")
                assert result.exit_code == 0
                assert result.stdout == "uname"
            finally:
                await transport.disconnect()

    async def test_a_dead_line_at_connect_names_the_last_bytes(self) -> None:
        async with open_pty_console(mute=True) as (device, _console):
            transport = SerialInterface(device=device, login_timeout=0.2)
            with pytest.raises(ConnectionError, match=r"timed out.*last bytes: ''"):
                await transport.connect()
            assert transport.connected is False

    async def test_a_prompt_printed_before_the_open_is_woken_by_enter(self) -> None:
        # F1: the console printed its prompt an hour ago (pyserial flushed it) and
        # only answers input. `emit_prompt=False` is that console: no banner, a
        # prompt for every line. Connect must poke, not just listen.
        async with open_pty_console(emit_prompt=False) as (device, _console):
            transport = SerialInterface(device=device, login_timeout=1.0)
            try:
                await transport.connect()
                result = await transport.exec("uname")
                assert result == ExecResult(stdout="uname", stderr="", exit_code=0)
            finally:
                await transport.disconnect()

    async def test_a_login_prompt_printed_before_the_open_is_woken_by_enter(self) -> None:
        # A getty re-prompts on an empty login; the poke must land before the
        # login exchange, not after it.
        async with open_pty_console(login_prompt="login: ", emit_prompt=False) as (
            device,
            _console,
        ):
            transport = SerialInterface(
                device=device,
                login_prompt=r"login: ",
                username="root",
                login_timeout=1.0,
            )
            try:
                await transport.connect()
                result = await transport.exec("uname")
                assert result.exit_code == 0
                assert result.stdout == "uname"
            finally:
                await transport.disconnect()

    async def test_the_prompt_the_poke_earns_does_not_shadow_the_first_exec(self) -> None:
        # The default console is already at its prompt when the poke lands, so
        # the wire carries two prompts. The first exec must match its own.
        async with open_pty_console() as (device, _console):
            transport = SerialInterface(device=device)
            try:
                await transport.connect()
                result = await transport.exec("fail")
                assert result == ExecResult(stdout="nope", stderr="", exit_code=1)
            finally:
                await transport.disconnect()


class TestPromptTimeout:
    async def test_a_wedged_console_settles_minus_one_naming_frozen(self) -> None:
        async with open_pty_console(hang=True) as (device, _console):
            transport = SerialInterface(device=device, prompt_timeout=0.3)
            try:
                await transport.connect()
                result = await transport.exec("uname")
                assert result.exit_code == -1
                assert "prompt timeout" in result.stderr
                assert "frozen" in result.stderr
                assert transport.connected is True
            finally:
                await transport.disconnect()

    async def test_a_slow_console_is_named_still_producing_bytes(self) -> None:
        async with open_pty_console(slow=True) as (device, _console):
            transport = SerialInterface(device=device, prompt_timeout=0.3)
            try:
                await transport.connect()
                result = await transport.exec("uname")
                assert result.exit_code == -1
                assert "prompt timeout" in result.stderr
                assert "still producing bytes" in result.stderr
            finally:
                await transport.disconnect()


class TestAbort:
    async def test_cancel_settles_in_flight_exec_as_transport_closed(self) -> None:
        async with open_pty_console() as (device, _console):
            transport = SerialInterface(device=device)
            try:
                await transport.connect()
                task = asyncio.create_task(transport.exec("sleep 30"))
                await asyncio.sleep(0.2)
                await transport.cancel_in_flight()
                result = await task
                assert result.exit_code == -1
                assert "transport closed" in result.stderr
                assert transport.connected is False
                with pytest.raises(ConnectionError, match="not connected"):
                    await transport.exec("uname")
            finally:
                await transport.disconnect()

    async def test_cancel_leaves_the_console_answering_the_next_connect(self) -> None:
        # The bench's detached console outlives a run. A parked command that
        # was cancelled must not leave it deaf for the next one.
        async with open_pty_console() as (device, _console):
            transport = SerialInterface(device=device, login_timeout=1.0, prompt_timeout=1.0)
            await transport.connect()
            parked = asyncio.create_task(transport.exec("sleep 30"))
            await asyncio.sleep(0.2)
            await transport.cancel_in_flight()
            assert (await parked).exit_code == -1
            again = SerialInterface(device=device, login_timeout=1.0, prompt_timeout=1.0)
            try:
                await again.connect()
                result = await again.exec("uname")
                assert result == ExecResult(stdout="uname", stderr="", exit_code=0)
            finally:
                await again.disconnect()

    async def test_truncated_exec_keeps_the_port_open(self) -> None:
        async with open_pty_console() as (device, _console):
            transport = SerialInterface(device=device, output_budget=64)
            try:
                await transport.connect()
                result = await transport.exec("flood")
                assert result.truncated is True
                assert result.exit_code == -1
                assert transport.connected is True
            finally:
                await transport.disconnect()


async def _until(condition: Callable[[], bool]) -> None:
    deadline = asyncio.get_running_loop().time() + 2
    while not condition():
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0)


class TestOneTurnAtATime:
    """Concurrent execs on one console run one at a time, in call order (issue #150)."""

    async def test_concurrent_execs_queue_and_settle_in_call_order(self) -> None:
        async with open_pty_console() as (device, console):
            transport = SerialInterface(device=device, prompt_timeout=5.0)
            await transport.connect()
            try:
                tasks = [
                    asyncio.create_task(transport.exec(command))
                    for command in ("delay 0.2", "fail", "uname")
                ]
                # Only the first is on the wire; the rest wait their turn, unsent.
                await _until(lambda: transport.queued == 2 and console.received == ["delay 0.2"])
                results = await asyncio.gather(*tasks)
            finally:
                await transport.disconnect()

        assert results == [
            ExecResult(stdout="woke", stderr="", exit_code=0),
            ExecResult(stdout="nope", stderr="", exit_code=1),
            ExecResult(stdout="uname", stderr="", exit_code=0),
        ]
        assert console.received == ["delay 0.2", "fail", "uname"]
        assert transport.queued == 0

    async def test_prompt_timeout_bounds_only_the_commands_own_wait(self) -> None:
        # Three 0.3 s commands behind a 0.7 s prompt_timeout: the queue takes
        # 0.9 s, no single command waits more than 0.3 s for its own prompt.
        async with open_pty_console() as (device, _console):
            transport = SerialInterface(device=device, prompt_timeout=0.7)
            await transport.connect()
            try:
                results = await asyncio.gather(*(transport.exec("delay 0.3") for _ in range(3)))
            finally:
                await transport.disconnect()

        assert [result.exit_code for result in results] == [0, 0, 0]
        assert [result.stdout for result in results] == ["woke", "woke", "woke"]

    @pytest.mark.parametrize("halt", ["cancel_in_flight", "disconnect"])
    async def test_halt_settles_the_queued_exec_without_sending_it(self, halt: str) -> None:
        async with open_pty_console() as (device, console):
            transport = SerialInterface(device=device, prompt_timeout=5.0)
            await transport.connect()
            try:
                parked = asyncio.create_task(transport.exec("sleep 30"))
                queued = asyncio.create_task(transport.exec("uname"))
                await _until(lambda: transport.queued == 1)
                await asyncio.sleep(0.2)

                await getattr(transport, halt)()

                assert (await parked).exit_code == -1
                result = await queued
                assert result == ExecResult(stdout="", stderr="transport closed", exit_code=-1)
                assert transport.connected is False
                assert transport.queued == 0
            finally:
                await transport.disconnect()
            self._assert_never_sent(console, "uname")

    @staticmethod
    def _assert_never_sent(console: FakeConsole, command: str) -> None:
        assert command not in console.received, console.received


class TestVia:
    async def test_exec_through_an_owned_ssh_hop(self) -> None:
        listener = await LoopbackSshd.start()
        hop = SshInterface(
            host="127.0.0.1",
            port=listener.port,
            username=listener.username,
            password=listener.password,
            skip_host_verification=True,
        )
        transport = SerialInterface(
            device="/dev/ttyUSB0",
            via=hop,
            bridge_command=SERIAL_BRIDGE_COMMAND,
        )
        try:
            await transport.connect()
            assert hop.connected is True
            result = await transport.exec("uname")
            assert result == ExecResult(stdout="uname", stderr="", exit_code=0)
            assert "127.0.0.1" in transport.connect_provenance
            assert transport.endpoint == "ttyUSB0"
            assert "ttyUSB0" in transport.route_label
        finally:
            await transport.disconnect()
            await listener.aclose()
        assert hop.connected is False

    async def test_cancel_does_not_collapse_the_via_hop(self) -> None:
        listener = await LoopbackSshd.start()
        hop = SshInterface(
            host="127.0.0.1",
            port=listener.port,
            username=listener.username,
            password=listener.password,
            skip_host_verification=True,
        )
        transport = SerialInterface(
            device="/dev/ttyUSB0",
            via=hop,
            bridge_command=SERIAL_BRIDGE_COMMAND,
        )
        try:
            await transport.connect()
            task = asyncio.create_task(transport.exec("sleep 30"))
            await asyncio.sleep(0.2)
            await transport.cancel_in_flight()
            result = await task
            assert result.exit_code == -1
            assert "transport closed" in result.stderr
            assert transport.connected is False
            assert hop.connected is True
            hop_result = await hop.exec("uname")
            assert hop_result.exit_code == 0
        finally:
            await transport.disconnect()
            await listener.aclose()

    async def test_a_dead_bridge_collapses_the_owned_hop(self) -> None:
        listener = await LoopbackSshd.start()
        hop = SshInterface(
            host="127.0.0.1",
            port=listener.port,
            username=listener.username,
            password=listener.password,
            skip_host_verification=True,
        )
        transport = SerialInterface(
            device="/dev/ttyUSB0",
            via=hop,
            bridge_command="not-a-console",
            login_timeout=0.5,
        )
        try:
            with pytest.raises(ConnectionError, match=r"closed|timed out"):
                await transport.connect()
            assert transport.connected is False
            assert hop.connected is False
        finally:
            await listener.aclose()

    async def test_a_failed_via_connect_collapses_the_hop(self) -> None:
        listener = await LoopbackSshd.start()
        hop = SshInterface(
            host="127.0.0.1",
            port=listener.port,
            username=listener.username,
            password="wrong",
            skip_host_verification=True,
        )
        transport = SerialInterface(
            device="/dev/ttyUSB0",
            via=hop,
            bridge_command=SERIAL_BRIDGE_COMMAND,
        )
        try:
            with pytest.raises(asyncssh.Error):
                await transport.connect()
            assert transport.connected is False
            assert hop.connected is False
        finally:
            await listener.aclose()

    async def test_a_pre_connected_via_is_rejected_and_left_untouched(self) -> None:
        listener = await LoopbackSshd.start()
        hop = SshInterface(
            host="127.0.0.1",
            port=listener.port,
            username=listener.username,
            password=listener.password,
            skip_host_verification=True,
        )
        transport = SerialInterface(
            device="/dev/ttyUSB0",
            via=hop,
            bridge_command=SERIAL_BRIDGE_COMMAND,
        )
        try:
            await hop.connect()
            with pytest.raises(ConnectionError, match="unconnected"):
                await transport.connect()
            assert transport.connected is False
            assert hop.connected is True
        finally:
            await hop.disconnect()
            await listener.aclose()


class TestTunnel:
    """A borrowed hop, the exec router's channel: handed over live, left standing."""

    async def test_exec_through_a_borrowed_ssh_hop(self) -> None:
        listener = await LoopbackSshd.start()
        hop = SshInterface(
            host="127.0.0.1",
            port=listener.port,
            username=listener.username,
            password=listener.password,
            skip_host_verification=True,
        )
        transport = SerialInterface(
            device="/dev/ttyUSB0",
            tunnel=hop,
            bridge_command=SERIAL_BRIDGE_COMMAND,
        )
        try:
            await hop.connect()
            await transport.connect()
            result = await transport.exec("uname")
            assert result == ExecResult(stdout="uname", stderr="", exit_code=0)
            assert transport.hop is hop
            assert "127.0.0.1" in transport.connect_provenance
            assert transport.route_label.endswith("ttyUSB0")
            await transport.disconnect()
            assert hop.connected is True
            assert (await hop.exec("uname")).exit_code == 0
        finally:
            await transport.disconnect()
            await hop.disconnect()
            await listener.aclose()

    async def test_an_unconnected_tunnel_is_rejected(self) -> None:
        hop = SshInterface(
            host="127.0.0.1",
            username="ops",
            password="jump",
            skip_host_verification=True,
        )
        transport = SerialInterface(device="/dev/ttyUSB0", tunnel=hop)

        with pytest.raises(ConnectionError, match="handed over connected"):
            await transport.connect()
        assert transport.connected is False

    async def test_a_dead_bridge_leaves_the_borrowed_hop_standing(self) -> None:
        listener = await LoopbackSshd.start()
        hop = SshInterface(
            host="127.0.0.1",
            port=listener.port,
            username=listener.username,
            password=listener.password,
            skip_host_verification=True,
        )
        transport = SerialInterface(
            device="/dev/ttyUSB0",
            tunnel=hop,
            bridge_command="not-a-console",
            login_timeout=0.5,
        )
        try:
            await hop.connect()
            with pytest.raises(ConnectionError, match=r"closed|timed out"):
                await transport.connect()
            assert transport.connected is False
            assert hop.connected is True
        finally:
            await hop.disconnect()
            await listener.aclose()

    def test_via_and_tunnel_are_mutually_exclusive(self) -> None:
        hop = SshInterface(host="127.0.0.1", username="ops", skip_host_verification=True)
        with pytest.raises(TypeError, match="mutually exclusive"):
            SerialInterface(device="/dev/ttyUSB0", via=hop, tunnel=hop)

    def test_tunnel_must_be_ssh(self) -> None:
        with pytest.raises(TypeError, match="SshInterface"):
            SerialInterface(device="/dev/ttyUSB0", tunnel="not-an-ssh")  # type: ignore[arg-type]


class TestMissingExtra:
    async def test_local_connect_names_the_extra_when_pyserial_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "serial_asyncio":
                raise ImportError("pyserial-asyncio is not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        transport = SerialInterface(device="/dev/ttyUSB0")
        with pytest.raises(ModuleNotFoundError, match="litmus\\[serial\\]"):
            await transport.connect()
        assert transport.connected is False
