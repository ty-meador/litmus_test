"""PLAN §7 target 6 / issue #41: in-flight termination at the transport.

Scattered coverage already lives in ``test_ssh``, ``test_executor``, ``test_mock``,
``test_parent_route``, ``test_remote``, and ``test_utils``. This module names the three
acceptance criteria and asserts the parts those files leave implied:

- hard-close is ``abort()`` at the asyncssh socket, not a graceful hangup
- SIGTERM reaches the host process group (SIGHUP from a dying shell is not enough)
- killing a hop collapses everything tunneled through it
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time
from pathlib import Path

import asyncssh
import pytest
from tests.loopback_sshd import LoopbackSshd

from litmus.executor import ExecResult, LocalExec
from litmus.ssh import SshInterface

_PROMPT_SECONDS = 1.0


def _password_ssh(
    peer: LoopbackSshd,
    *,
    username: str | None = None,
    password: str | None = None,
    via: SshInterface | None = None,
    tunnel: SshInterface | None = None,
) -> SshInterface:
    resolved = password if password is not None else peer.password
    assert resolved is not None
    return SshInterface(
        host="127.0.0.1",
        port=peer.port,
        username=username if username is not None else peer.username,
        password=resolved,
        skip_host_verification=True,
        via=via,
        tunnel=tunnel,
    )


async def _start_remote_sleep(ssh: SshInterface) -> asyncio.Task[ExecResult]:
    task = asyncio.create_task(ssh.exec("sleep 30"))
    await asyncio.sleep(0.2)
    return task


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


class _HostProcess:
    """Probe helpers for in-flight host process groups. Not production API."""

    @staticmethod
    def sigterm_trap_command(pid_path: Path, marker_path: Path, seconds: int = 60) -> str:
        # Ignore SIGHUP so a shell-only kill cannot masquerade as group SIGTERM.
        # Re-raise SIGTERM after the marker so settlement still sees a signal death.
        script = (
            "import os, pathlib, signal, time\n"
            f"pid_path = pathlib.Path({str(pid_path)!r})\n"
            f"marker_path = pathlib.Path({str(marker_path)!r})\n"
            "def on_term(signum, frame):\n"
            "    marker_path.write_text('TERM')\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
            "    os.kill(os.getpid(), signal.SIGTERM)\n"
            "signal.signal(signal.SIGTERM, on_term)\n"
            "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
            "pid_path.write_text(str(os.getpid()))\n"
            f"time.sleep({seconds})\n"
        )
        return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    @staticmethod
    def is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return not _HostProcess.is_zombie(pid)

    @staticmethod
    def is_zombie(pid: int) -> bool:
        stat = Path(f"/proc/{pid}/stat")
        try:
            body = stat.read_text()
        except FileNotFoundError, ProcessLookupError:
            return False
        state = body.rsplit(")", 1)[-1].split()[0]
        return state == "Z"

    @staticmethod
    def read_pid(pid_path: Path) -> int | None:
        if not pid_path.exists():
            return None
        text = pid_path.read_text().strip()
        if text:
            return int(text)
        return None

    @staticmethod
    async def wait_for_pidfile(pid_path: Path, seconds: float = 2.0) -> int:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pid = _HostProcess.read_pid(pid_path)
            if pid is not None:
                return pid
            await asyncio.sleep(0.02)
        raise AssertionError("child never wrote its pid")

    @staticmethod
    async def wait_until_gone(pid: int, seconds: float = 2.0) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not _HostProcess.is_alive(pid) and not _HostProcess.is_zombie(pid):
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"pid {pid} did not leave the process table")


class TestHardCloseAtTheTransportLayer:
    """AC: hard-close asserted at the transport layer."""

    async def test_cancel_aborts_the_asyncssh_socket_and_fails_in_flight_exec_promptly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        aborts = _AbortCounter()
        aborts.install(monkeypatch)
        peer = await LoopbackSshd.start()
        ssh = _password_ssh(peer)
        try:
            await ssh.connect()
            task = await _start_remote_sleep(ssh)
            await ssh.cancel_in_flight()
            result = await asyncio.wait_for(task, timeout=_PROMPT_SECONDS)
            assert aborts.calls >= 1
            assert result.exit_code == -1
            assert ssh.connected is False
            with pytest.raises(ConnectionError, match="not connected"):
                await ssh.exec("uname")
        finally:
            await ssh.disconnect()
            await peer.aclose()


class TestSigtermPropagationToChildProcesses:
    """AC: SIGTERM propagation to child processes proven."""

    async def test_cancel_in_flight_sigterms_the_process_group_not_just_the_shell(
        self, tmp_path: Path
    ) -> None:
        host = LocalExec()
        pid_path = tmp_path / "pid"
        marker_path = tmp_path / "signal"
        task = asyncio.create_task(
            host.run(_HostProcess.sigterm_trap_command(pid_path, marker_path))
        )
        pid = await _HostProcess.wait_for_pidfile(pid_path)
        await host.cancel_in_flight()
        result = await asyncio.wait_for(task, timeout=_PROMPT_SECONDS)
        assert result.exit_code == -1
        assert marker_path.read_text() == "TERM"
        await _HostProcess.wait_until_gone(pid)


class TestViaChainCollapseEndToEnd:
    """AC: via-chain collapse asserted end to end."""

    async def test_owner_hard_close_collapses_tunneled_in_flight_exec(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        aborts = _AbortCounter()
        aborts.install(monkeypatch)
        jump_peer = await LoopbackSshd.start(username="ops", password="jump")
        target_peer = await LoopbackSshd.start(username="root", password="admin")
        jump = _password_ssh(jump_peer, username="ops", password="jump")
        target = _password_ssh(target_peer, username="root", password="admin", tunnel=jump)
        try:
            await jump.connect()
            await target.connect()
            task = await _start_remote_sleep(target)
            await jump.cancel_in_flight()
            result = await asyncio.wait_for(task, timeout=_PROMPT_SECONDS)
            assert result.exit_code == -1
            assert jump.connected is False
            assert aborts.calls >= 1
        finally:
            await target.disconnect()
            await jump.disconnect()
            await jump_peer.aclose()
            await target_peer.aclose()
