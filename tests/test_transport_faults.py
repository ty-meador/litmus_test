"""Transport fault-injection matrix against the loopback sshd.

Issue #66. Each row pins the settled `ExecResult` or the raised failure class,
the stderr/cause line, the connected flag, and that `cancel_in_flight`
afterwards is idempotent. The surface is `SshInterface`: `utils.exec` never
throws (author-facing), but the protocol stays loud.

MockTransport already covers generic connect failure and park+cancel in
`test_mock.py` (raises `ConnectionError`). Those rows are cross-checked here
on the real asyncssh path, not copied. SSH mid-command death settles as
`ExecResult`; the mock raises from the parked future.

Command exceeding the exec budget is owned by issue #62 / MR !84. That
row is cross-checked here against the same loopback path, with the matrix's
connected-flag and idempotent-cancel pins, not copied from `test_ssh.py`.
"""

from __future__ import annotations

import asyncio
import socket

import asyncssh
import pytest
from tests.loopback_sshd import LoopbackSshd

from litmus.executor import DEFAULT_EXEC_OUTPUT_BUDGET
from litmus.ssh import SshInterface

_SETTLE_SECONDS = 2.0


def _password_ssh(
    peer: LoopbackSshd,
    *,
    username: str | None = None,
    password: str | None = None,
    via: SshInterface | None = None,
    skip_host_verification: bool = True,
    output_budget: int = DEFAULT_EXEC_OUTPUT_BUDGET,
) -> SshInterface:
    resolved = password if password is not None else peer.password
    assert resolved is not None
    return SshInterface(
        host="127.0.0.1",
        port=peer.port,
        username=username if username is not None else peer.username,
        password=resolved,
        skip_host_verification=skip_host_verification,
        via=via,
        output_budget=output_budget,
    )


async def _assert_cancel_idempotent(ssh: SshInterface) -> None:
    await ssh.cancel_in_flight()
    await ssh.cancel_in_flight()
    assert ssh.connected is False


class TestTransportFaultMatrix:
    """One test per row of issue #66, against the real asyncssh loopback path."""

    async def test_connection_refused(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        ssh = SshInterface(
            host="127.0.0.1",
            port=dead_port,
            username="root",
            password="admin",
            skip_host_verification=True,
        )
        with pytest.raises(ConnectionRefusedError, match="Connect call failed") as refused:
            await ssh.connect()
        assert "127.0.0.1" in str(refused.value)
        assert ssh.connected is False
        await _assert_cancel_idempotent(ssh)

    async def test_authentication_rejected(self) -> None:
        peer = await LoopbackSshd.start()
        ssh = _password_ssh(peer, password="wrong")
        try:
            with pytest.raises(asyncssh.PermissionDenied, match="Permission denied") as denied:
                await ssh.connect()
            assert peer.username in str(denied.value)
            assert ssh.connected is False
            await _assert_cancel_idempotent(ssh)
        finally:
            await peer.aclose()

    async def test_host_key_mismatch(self) -> None:
        peer = await LoopbackSshd.start()
        assert peer.password is not None
        ssh = SshInterface(
            host="127.0.0.1",
            port=peer.port,
            username=peer.username,
            password=peer.password,
        )
        try:
            with pytest.raises(
                asyncssh.HostKeyNotVerifiable, match="Host key is not trusted"
            ) as mismatch:
                await ssh.connect()
            assert "127.0.0.1" in str(mismatch.value)
            assert ssh.connected is False
            await _assert_cancel_idempotent(ssh)
        finally:
            await peer.aclose()

    async def test_drop_during_connect(self) -> None:
        peer = await LoopbackSshd.start(drop_on_connect=True)
        ssh = _password_ssh(peer)
        try:
            with pytest.raises(ConnectionResetError, match="Connection reset by peer"):
                await ssh.connect()
            assert ssh.connected is False
            await _assert_cancel_idempotent(ssh)
        finally:
            await peer.aclose()

    async def test_drop_mid_command(self) -> None:
        peer = await LoopbackSshd.start()
        ssh = _password_ssh(peer)
        try:
            await ssh.connect()
            task = asyncio.create_task(ssh.exec("sleep 30"))
            await asyncio.sleep(0.2)
            peer.abort_connections()
            result = await asyncio.wait_for(task, timeout=_SETTLE_SECONDS)
            assert result.exit_code == -1
            assert result.stderr.splitlines()[0] == "transport closed"
            assert ssh.connected is True
            await _assert_cancel_idempotent(ssh)
        finally:
            await ssh.disconnect()
            await peer.aclose()

    async def test_remote_closes_cleanly_mid_run(self) -> None:
        peer = await LoopbackSshd.start()
        ssh = _password_ssh(peer)
        try:
            await ssh.connect()
            task = asyncio.create_task(ssh.exec("sleep 30"))
            await asyncio.sleep(0.2)
            peer.close_connections()
            result = await asyncio.wait_for(task, timeout=_SETTLE_SECONDS)
            assert result.exit_code == -1
            assert result.stderr.splitlines()[0] == "transport closed"
            assert ssh.connected is True
            await _assert_cancel_idempotent(ssh)
        finally:
            await ssh.disconnect()
            await peer.aclose()

    async def test_stalled_output(self) -> None:
        peer = await LoopbackSshd.start()
        ssh = _password_ssh(peer)
        try:
            await ssh.connect()
            task = asyncio.create_task(ssh.exec("stall"))
            await asyncio.sleep(0.3)
            assert not task.done()
            await ssh.cancel_in_flight()
            result = await asyncio.wait_for(task, timeout=_SETTLE_SECONDS)
            assert result.exit_code == -1
            assert result.stderr.splitlines()[0] == "transport closed"
            assert ssh.connected is False
            await _assert_cancel_idempotent(ssh)
        finally:
            await ssh.disconnect()
            await peer.aclose()

    async def test_non_utf8_bytes(self) -> None:
        peer = await LoopbackSshd.start(encoding=None)
        ssh = _password_ssh(peer)
        try:
            await ssh.connect()
            result = await asyncio.wait_for(ssh.exec("non-utf8"), timeout=_SETTLE_SECONDS)
            assert result.exit_code == 0
            assert result.stdout == "pre\ufffdpost"
            assert result.stderr == ""
            assert ssh.connected is True
            await _assert_cancel_idempotent(ssh)
        finally:
            await ssh.disconnect()
            await peer.aclose()

    async def test_binary_output(self) -> None:
        peer = await LoopbackSshd.start(encoding=None)
        ssh = _password_ssh(peer)
        try:
            await ssh.connect()
            result = await asyncio.wait_for(ssh.exec("binary"), timeout=_SETTLE_SECONDS)
            assert result.exit_code == 0
            assert result.stdout == "\x00hello\x01world"
            assert result.stderr == ""
            assert ssh.connected is True
            await _assert_cancel_idempotent(ssh)
        finally:
            await ssh.disconnect()
            await peer.aclose()

    async def test_stderr_flood(self) -> None:
        peer = await LoopbackSshd.start()
        ssh = _password_ssh(peer)
        try:
            await ssh.connect()
            result = await asyncio.wait_for(ssh.exec("dump-err 65536"), timeout=_SETTLE_SECONDS)
            assert result.exit_code == 0
            assert result.stdout == ""
            assert result.stderr == "y" * 65536
            assert result.truncated is False
            assert ssh.connected is True
            await _assert_cancel_idempotent(ssh)
        finally:
            await ssh.disconnect()
            await peer.aclose()

    async def test_command_exceeding_the_exec_budget(self) -> None:
        peer = await LoopbackSshd.start()
        ssh = _password_ssh(peer, output_budget=64)
        try:
            await ssh.connect()
            result = await asyncio.wait_for(ssh.exec("dump 10000"), timeout=_SETTLE_SECONDS)
            assert result.truncated is True
            assert result.captured_bytes == 64
            assert result.stdout == "x" * 64
            assert ssh.connected is True
            await _assert_cancel_idempotent(ssh)
        finally:
            await ssh.disconnect()
            await peer.aclose()

    async def test_via_chain_hop_dying_while_leaf_is_live(self) -> None:
        jump_peer = await LoopbackSshd.start(username="ops", password="jump")
        leaf_peer = await LoopbackSshd.start(username="root", password="admin")
        jump = _password_ssh(jump_peer, username="ops", password="jump")
        leaf = _password_ssh(leaf_peer, username="root", password="admin", via=jump)
        try:
            await leaf.connect()
            task = asyncio.create_task(leaf.exec("sleep 30"))
            await asyncio.sleep(0.3)
            jump_peer.abort_connections()
            result = await asyncio.wait_for(task, timeout=_SETTLE_SECONDS)
            assert result.exit_code == -1
            assert result.stderr.splitlines()[0] == "transport closed"
            assert leaf.connected is True
            assert jump.connected is True
            await _assert_cancel_idempotent(leaf)
            assert jump.connected is False
        finally:
            await leaf.disconnect()
            await jump_peer.aclose()
            await leaf_peer.aclose()
