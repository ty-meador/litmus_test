"""SshInterface: asyncssh transport with hard close and via chains."""

from __future__ import annotations

import asyncio
import os
import signal
import stat
from pathlib import Path

import asyncssh
import pytest

from litmus import PASS, LitmusTest, TestUtils, Verdict
from litmus.executor import ExecResult
from litmus.lifecycle import State
from litmus.remote import RemoteInterface
from litmus.routing import ExecRouter, ExecRouting, ShellOpenRequest, SshConnector
from litmus.ssh import SshInterface


class _LoopbackSshd:
    """In-process sshd so self-tests never need a machine's sshd."""

    def __init__(
        self,
        listener: asyncssh.SSHAcceptor,
        *,
        username: str,
        password: str | None,
    ) -> None:
        self._listener = listener
        self.username = username
        self.password = password

    @property
    def port(self) -> int:
        return self._listener.get_port()

    async def aclose(self) -> None:
        self._listener.close()
        await self._listener.wait_closed()

    @classmethod
    async def start(
        cls,
        *,
        username: str = "root",
        password: str | None = "admin",
        authorized_key: asyncssh.SSHKey | None = None,
    ) -> _LoopbackSshd:
        host_key = _LoopbackSshd.ed25519()
        expected_user = username
        expected_password = password
        authorized = authorized_key

        class _Auth(asyncssh.SSHServer):
            def begin_auth(self, username: str) -> bool:
                return True

            def password_auth_supported(self) -> bool:
                return expected_password is not None

            def public_key_auth_supported(self) -> bool:
                return authorized is not None

            def validate_password(self, username: str, password: str) -> bool:
                return (
                    expected_password is not None
                    and username == expected_user
                    and password == expected_password
                )

            def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
                if authorized is None or username != expected_user:
                    return False
                return key.export_public_key() == authorized.export_public_key()

            def connection_requested(
                self, dest_host: str, dest_port: int, orig_host: str, orig_port: int
            ) -> bool:
                return True

        listener = await asyncssh.listen(
            "127.0.0.1",
            0,
            server_host_keys=[host_key],
            server_factory=_Auth,
            process_factory=_LoopbackSshd._handle,
            encoding="utf-8",
        )
        return cls(listener, username=username, password=password)

    @staticmethod
    def ed25519() -> asyncssh.SSHKey:
        return asyncssh.generate_private_key("ssh-ed25519")  # pyright: ignore[reportUnknownMemberType]

    @staticmethod
    def parse_agent(script: str) -> tuple[str, int]:
        sock = ""
        pid = 0
        for line in script.splitlines():
            if line.startswith("SSH_AUTH_SOCK="):
                sock = line.split("=", 1)[1].split(";")[0]
            elif line.startswith("SSH_AGENT_PID="):
                pid = int(line.split("=", 1)[1].split(";")[0])
        if not sock or not pid:
            pytest.skip(f"could not parse ssh-agent output: {script!r}")
        return sock, pid

    @staticmethod
    async def _handle(process: asyncssh.SSHServerProcess[str]) -> None:
        command = process.command or ""
        if command == "fail":
            process.stderr.write("nope")
            process.exit(1)
        elif command.startswith("sleep "):
            try:
                await asyncio.sleep(float(command.split()[1]))
                process.stdout.write("woke")
                process.exit(0)
            except asyncio.CancelledError:
                process.exit(255)
                raise
        else:
            process.stdout.write(command)
            process.exit(0)
        process.close()
        await process.wait_closed()


class TestPublicSurface:
    def test_ssh_interface_is_not_on_the_public_surface(self) -> None:
        import litmus

        assert not hasattr(litmus, "SshInterface")
        assert "SshInterface" not in litmus.__all__

    def test_is_a_remote_interface(self) -> None:
        transport = SshInterface(
            host="127.0.0.1", username="root", password="admin", skip_host_verification=True
        )

        assert isinstance(transport, RemoteInterface)


class TestAuthResolution:
    def test_explicit_agent_wins_over_password(self) -> None:
        transport = SshInterface(
            host="127.0.0.1",
            username="ops",
            password="admin",
            agent="/tmp/agent.sock",
            skip_host_verification=True,
        )

        assert transport.auth_label == "agent"

    def test_key_file_wins_over_password(self) -> None:
        transport = SshInterface(
            host="127.0.0.1",
            username="ops",
            password="admin",
            private_key_path="/tmp/id_ed25519",
            skip_host_verification=True,
        )

        assert transport.auth_label == "key-file"

    async def test_connect_without_credentials_or_agent_raises(self) -> None:
        transport = SshInterface(host="127.0.0.1", username="ops", skip_host_verification=True)
        original = os.environ.pop("SSH_AUTH_SOCK", None)
        try:
            with pytest.raises(ConnectionError, match="SSH_AUTH_SOCK"):
                await transport.connect()
        finally:
            if original is not None:
                os.environ["SSH_AUTH_SOCK"] = original
            assert transport.connected is False


class TestPasswordExec:
    async def test_command_output_is_evidence(self) -> None:
        peer = await _LoopbackSshd.start()
        ssh = SshInterface(
            host="127.0.0.1",
            port=peer.port,
            username=peer.username,
            password=peer.password,
            skip_host_verification=True,
        )
        try:
            await ssh.connect()
            result = await ssh.exec("uname")
            assert result == ExecResult(stdout="uname", stderr="", exit_code=0)
            assert ssh.connected is True
        finally:
            await ssh.disconnect()
            await peer.aclose()

    async def test_a_failed_command_is_evidence_not_an_exception(self) -> None:
        peer = await _LoopbackSshd.start()
        ssh = SshInterface(
            host="127.0.0.1",
            port=peer.port,
            username=peer.username,
            password=peer.password,
            skip_host_verification=True,
        )
        try:
            await ssh.connect()
            result = await ssh.exec("fail")
            assert result.exit_code == 1
            assert result.stderr == "nope"
        finally:
            await ssh.disconnect()
            await peer.aclose()

    async def test_exec_before_connect_rejects(self) -> None:
        ssh = SshInterface(
            host="127.0.0.1", username="root", password="admin", skip_host_verification=True
        )

        with pytest.raises(ConnectionError, match="not connected"):
            await ssh.exec("uname")

    async def test_disconnect_is_safe_when_never_connected(self) -> None:
        ssh = SshInterface(
            host="127.0.0.1", username="root", password="admin", skip_host_verification=True
        )

        await ssh.disconnect()

        assert ssh.connected is False


class TestHardClose:
    async def test_abort_settles_in_flight_exec_at_minus_one(self) -> None:
        peer = await _LoopbackSshd.start()
        ssh = SshInterface(
            host="127.0.0.1",
            port=peer.port,
            username=peer.username,
            password=peer.password,
            skip_host_verification=True,
        )
        try:
            await ssh.connect()
            task = asyncio.create_task(ssh.exec("sleep 30"))
            await asyncio.sleep(0.2)
            await ssh.cancel_in_flight()
            result = await task
            assert result.exit_code == -1
            assert ssh.connected is False
            with pytest.raises(ConnectionError, match="not connected"):
                await ssh.exec("uname")
        finally:
            await ssh.disconnect()
            await peer.aclose()

    async def test_cancel_is_safe_when_idle(self) -> None:
        peer = await _LoopbackSshd.start()
        ssh = SshInterface(
            host="127.0.0.1",
            port=peer.port,
            username=peer.username,
            password=peer.password,
            skip_host_verification=True,
        )
        try:
            await ssh.connect()
            await ssh.cancel_in_flight()
            await ssh.cancel_in_flight()
            assert ssh.connected is False
        finally:
            await ssh.disconnect()
            await peer.aclose()

    async def test_cancel_is_safe_when_never_connected(self) -> None:
        ssh = SshInterface(
            host="127.0.0.1", username="root", password="admin", skip_host_verification=True
        )

        await ssh.cancel_in_flight()

        assert ssh.connected is False


class TestKeyAuth:
    async def test_in_memory_private_key(self) -> None:
        client_key = _LoopbackSshd.ed25519()
        peer = await _LoopbackSshd.start(username="ops", password=None, authorized_key=client_key)
        ssh = SshInterface(
            host="127.0.0.1",
            port=peer.port,
            username="ops",
            private_key=client_key.export_private_key("openssh"),
            skip_host_verification=True,
        )
        try:
            await ssh.connect()
            result = await ssh.exec("id")
            assert result == ExecResult(stdout="id", stderr="", exit_code=0)
            assert ssh.auth_label == "key"
        finally:
            await ssh.disconnect()
            await peer.aclose()

    async def test_private_key_path(self, tmp_path: Path) -> None:
        client_key = _LoopbackSshd.ed25519()
        key_file = tmp_path / "id_ed25519"
        key_file.write_bytes(client_key.export_private_key("openssh"))
        key_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
        peer = await _LoopbackSshd.start(username="ops", password=None, authorized_key=client_key)
        ssh = SshInterface(
            host="127.0.0.1",
            port=peer.port,
            username="ops",
            private_key_path=key_file,
            skip_host_verification=True,
        )
        try:
            await ssh.connect()
            result = await ssh.exec("id")
            assert result.exit_code == 0
            assert ssh.auth_label == "key-file"
        finally:
            await ssh.disconnect()
            await peer.aclose()

    async def test_agent_path(self, tmp_path: Path) -> None:
        client_key = _LoopbackSshd.ed25519()
        key_file = tmp_path / "id_ed25519"
        key_file.write_bytes(client_key.export_private_key("openssh"))
        key_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

        agent = await asyncio.create_subprocess_exec(
            "ssh-agent",
            "-s",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await agent.communicate()
        if agent.returncode != 0:
            pytest.skip(f"ssh-agent unavailable: {stderr.decode()}")
        sock, pid = _LoopbackSshd.parse_agent(stdout.decode())
        try:
            added = await asyncio.create_subprocess_exec(
                "ssh-add",
                str(key_file),
                env={**os.environ, "SSH_AUTH_SOCK": sock, "SSH_AGENT_PID": str(pid)},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _add_out, add_err = await added.communicate()
            if added.returncode != 0:
                pytest.skip(f"ssh-add failed: {add_err.decode()}")

            peer = await _LoopbackSshd.start(
                username="ops", password=None, authorized_key=client_key
            )
            ssh = SshInterface(
                host="127.0.0.1",
                port=peer.port,
                username="ops",
                agent=sock,
                skip_host_verification=True,
            )
            try:
                await ssh.connect()
                result = await ssh.exec("id")
                assert result.exit_code == 0
                assert ssh.auth_label == "agent"
            finally:
                await ssh.disconnect()
                await peer.aclose()
        finally:
            os.kill(pid, signal.SIGTERM)


class TestViaChain:
    async def test_target_tunnels_through_jump_and_execs(self) -> None:
        jump_peer = await _LoopbackSshd.start(username="ops", password="jump")
        target_peer = await _LoopbackSshd.start(username="root", password="admin")
        jump = SshInterface(
            host="127.0.0.1",
            port=jump_peer.port,
            username="ops",
            password="jump",
            skip_host_verification=True,
        )
        target = SshInterface(
            host="127.0.0.1",
            port=target_peer.port,
            username="root",
            password="admin",
            skip_host_verification=True,
            via=jump,
        )
        try:
            await target.connect()
            assert jump.connected is True
            assert target.connected is True
            result = await target.exec("uname")
            assert result == ExecResult(stdout="uname", stderr="", exit_code=0)
        finally:
            await target.disconnect()
            await jump_peer.aclose()
            await target_peer.aclose()
        assert jump.connected is False
        assert target.connected is False

    async def test_cancel_collapses_the_via_chain(self) -> None:
        jump_peer = await _LoopbackSshd.start(username="ops", password="jump")
        target_peer = await _LoopbackSshd.start(username="root", password="admin")
        jump = SshInterface(
            host="127.0.0.1",
            port=jump_peer.port,
            username="ops",
            password="jump",
            skip_host_verification=True,
        )
        target = SshInterface(
            host="127.0.0.1",
            port=target_peer.port,
            username="root",
            password="admin",
            skip_host_verification=True,
            via=jump,
        )
        try:
            await target.connect()
            task = asyncio.create_task(target.exec("sleep 30"))
            await asyncio.sleep(0.2)
            await target.cancel_in_flight()
            result = await task
            assert result.exit_code == -1
            assert target.connected is False
            assert jump.connected is False
        finally:
            await target.disconnect()
            await jump_peer.aclose()
            await target_peer.aclose()

    async def test_a_pre_connected_via_is_rejected_and_left_untouched(self) -> None:
        jump_peer = await _LoopbackSshd.start(username="ops", password="jump")
        jump = SshInterface(
            host="127.0.0.1",
            port=jump_peer.port,
            username="ops",
            password="jump",
            skip_host_verification=True,
        )
        target = SshInterface(
            host="127.0.0.1",
            port=jump_peer.port,
            username="root",
            password="admin",
            skip_host_verification=True,
            via=jump,
        )
        try:
            await jump.connect()
            with pytest.raises(ConnectionError, match="unconnected"):
                await target.connect()
            assert target.connected is False
            assert jump.connected is True
        finally:
            await jump.disconnect()
            await jump_peer.aclose()

    async def test_a_failed_target_connect_collapses_the_via_it_opened(self) -> None:
        jump_peer = await _LoopbackSshd.start(username="ops", password="jump")
        target_peer = await _LoopbackSshd.start(username="root", password="admin")
        jump = SshInterface(
            host="127.0.0.1",
            port=jump_peer.port,
            username="ops",
            password="jump",
            skip_host_verification=True,
        )
        target = SshInterface(
            host="127.0.0.1",
            port=target_peer.port,
            username="root",
            password="wrong",
            skip_host_verification=True,
            via=jump,
        )
        try:
            with pytest.raises(asyncssh.Error):
                await target.connect()
            assert target.connected is False
            assert jump.connected is False
        finally:
            await jump_peer.aclose()
            await target_peer.aclose()


class TestBorrowedTunnel:
    async def test_target_tunnels_through_a_borrowed_live_hop(self) -> None:
        jump_peer = await _LoopbackSshd.start(username="ops", password="jump")
        target_peer = await _LoopbackSshd.start(username="root", password="admin")
        jump = SshInterface(
            host="127.0.0.1",
            port=jump_peer.port,
            username="ops",
            password="jump",
            skip_host_verification=True,
        )
        target = SshInterface(
            host="127.0.0.1",
            port=target_peer.port,
            username="root",
            password="admin",
            skip_host_verification=True,
            tunnel=jump,
        )
        try:
            await jump.connect()
            await target.connect()
            result = await target.exec("uname")
            assert result == ExecResult(stdout="uname", stderr="", exit_code=0)
            await target.disconnect()
            assert target.connected is False
            assert jump.connected is True
            result = await jump.exec("id")
            assert result.exit_code == 0
        finally:
            await jump.disconnect()
            await jump_peer.aclose()
            await target_peer.aclose()

    async def test_an_unconnected_tunnel_is_rejected_and_left_untouched(self) -> None:
        jump_peer = await _LoopbackSshd.start(username="ops", password="jump")
        jump = SshInterface(
            host="127.0.0.1",
            port=jump_peer.port,
            username="ops",
            password="jump",
            skip_host_verification=True,
        )
        target = SshInterface(
            host="127.0.0.1",
            port=jump_peer.port,
            username="root",
            password="admin",
            skip_host_verification=True,
            tunnel=jump,
        )
        try:
            with pytest.raises(ConnectionError, match="handed over connected"):
                await target.connect()
            assert target.connected is False
            assert jump.connected is False
        finally:
            await jump_peer.aclose()

    def test_via_and_tunnel_are_mutually_exclusive(self) -> None:
        hop = SshInterface(
            host="127.0.0.1",
            username="ops",
            password="jump",
            skip_host_verification=True,
        )
        with pytest.raises(ValueError, match="mutually exclusive"):
            SshInterface(
                host="127.0.0.1",
                username="root",
                password="admin",
                skip_host_verification=True,
                via=hop,
                tunnel=hop,
            )

    async def test_cancel_leaves_the_borrowed_hop_up(self) -> None:
        jump_peer = await _LoopbackSshd.start(username="ops", password="jump")
        target_peer = await _LoopbackSshd.start(username="root", password="admin")
        jump = SshInterface(
            host="127.0.0.1",
            port=jump_peer.port,
            username="ops",
            password="jump",
            skip_host_verification=True,
        )
        target = SshInterface(
            host="127.0.0.1",
            port=target_peer.port,
            username="root",
            password="admin",
            skip_host_verification=True,
            tunnel=jump,
        )
        try:
            await jump.connect()
            await target.connect()
            task = asyncio.create_task(target.exec("sleep 30"))
            await asyncio.sleep(0.2)
            await target.cancel_in_flight()
            result = await task
            assert result.exit_code == -1
            assert target.connected is False
            assert jump.connected is True
            result = await jump.exec("id")
            assert result.exit_code == 0
        finally:
            await jump.disconnect()
            await jump_peer.aclose()
            await target_peer.aclose()

    async def test_a_failed_target_connect_leaves_the_borrowed_hop_up(self) -> None:
        jump_peer = await _LoopbackSshd.start(username="ops", password="jump")
        target_peer = await _LoopbackSshd.start(username="root", password="admin")
        jump = SshInterface(
            host="127.0.0.1",
            port=jump_peer.port,
            username="ops",
            password="jump",
            skip_host_verification=True,
        )
        target = SshInterface(
            host="127.0.0.1",
            port=target_peer.port,
            username="root",
            password="wrong",
            skip_host_verification=True,
            tunnel=jump,
        )
        try:
            await jump.connect()
            with pytest.raises(asyncssh.Error):
                await target.connect()
            assert target.connected is False
            assert jump.connected is True
        finally:
            await jump.disconnect()
            await jump_peer.aclose()
            await target_peer.aclose()


class TestRoutedConnector:
    """The real `SshConnector` against the ownership contract.

    The `_Connector` seam in test_routing covers routing policy only; these
    tests prove the routed tunnel path holds against a live transport — the
    seam is what let a via-owning connector ship green once.
    """

    async def test_opens_through_a_live_via_and_borrows_it(self) -> None:
        jump_peer = await _LoopbackSshd.start(username="ops", password="jump")
        target_peer = await _LoopbackSshd.start(username="root", password="admin")
        jump = SshInterface(
            host="127.0.0.1",
            port=jump_peer.port,
            username="ops",
            password="jump",
            skip_host_verification=True,
        )
        try:
            await jump.connect()
            request = ShellOpenRequest(
                host="127.0.0.1",
                username="root",
                port=target_peer.port,
                password="admin",
                skip_host_verification=True,
                via=jump,
            )
            target = await SshConnector().open(request)
            result = await target.exec("uname")
            assert result == ExecResult(stdout="uname", stderr="", exit_code=0)
            await target.disconnect()
            assert target.connected is False
            assert jump.connected is True
            result = await jump.exec("id")
            assert result.exit_code == 0
        finally:
            await jump.disconnect()
            await jump_peer.aclose()
            await target_peer.aclose()

    async def test_inherited_connection_form_tunnels_through_the_live_parent_hop(
        self,
    ) -> None:
        jump_peer = await _LoopbackSshd.start(username="ops", password="jump")
        target_peer = await _LoopbackSshd.start(username="root", password="admin")
        jump = SshInterface(
            host="127.0.0.1",
            port=jump_peer.port,
            username="ops",
            password="jump",
            skip_host_verification=True,
        )

        async def local_exec(command: str) -> ExecResult:
            return ExecResult(stdout=f"local:{command}", stderr="", exit_code=0)

        try:
            await jump.connect()
            routing = ExecRouting(
                local_exec,
                inherited=ExecRouter.for_ssh(jump),
                connector=SshConnector(),
            )
            shell = await routing.use_shell(
                host="127.0.0.1",
                username="root",
                port=target_peer.port,
                password="admin",
                skip_host_verification=True,
            )
            result = await shell.exec("uname")
            assert result == ExecResult(stdout="uname", stderr="", exit_code=0)
            # The router owns hop lifecycle: dependents close before hops,
            # and closing what this node opened leaves the borrowed hop up.
            for opened in routing.opened:
                await opened.disconnect()
            assert jump.connected is True
        finally:
            await jump.disconnect()
            await jump_peer.aclose()
            await target_peer.aclose()


class TestReconnect:
    async def test_reconnect_to_replaces_the_socket_at_a_new_address(self) -> None:
        first = await _LoopbackSshd.start()
        second = await _LoopbackSshd.start()
        ssh = SshInterface(
            host="127.0.0.1",
            port=first.port,
            username=first.username,
            password=first.password,
            skip_host_verification=True,
        )
        try:
            await ssh.connect()
            assert (await ssh.exec("one")).stdout == "one"
            await ssh.reconnect_to("127.0.0.1", port=second.port)
            assert ssh.connected is True
            assert (await ssh.exec("two")).stdout == "two"
        finally:
            await ssh.disconnect()
            await first.aclose()
            await second.aclose()


class TestAuthProvenance:
    """The credential path is named exactly, and the credential value never is."""

    def test_a_key_file_names_the_file_it_will_read(self) -> None:
        transport = SshInterface(
            host="10.0.0.1",
            username="ops",
            private_key_path="/home/ops/.ssh/id_ed25519",
            skip_host_verification=True,
        )

        assert transport.auth_label == "key-file"
        assert transport.connect_provenance == (
            "ssh connect: ops@10.0.0.1:22 auth=key-file(/home/ops/.ssh/id_ed25519) via=direct"
        )

    def test_an_explicit_agent_names_its_socket(self) -> None:
        transport = SshInterface(
            host="10.0.0.1",
            username="ops",
            agent="/run/user/1000/keyring/ssh",
            skip_host_verification=True,
        )

        assert transport.connect_provenance == (
            "ssh connect: ops@10.0.0.1:22 auth=agent(/run/user/1000/keyring/ssh) via=direct"
        )

    def test_the_inherited_agent_socket_is_named_as_the_path_selected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/inherited.sock")
        transport = SshInterface(host="10.0.0.1", username="ops", skip_host_verification=True)

        assert transport.auth_label == "agent"
        assert "auth=agent(/run/user/1000/inherited.sock)" in transport.connect_provenance

    def test_a_password_is_named_by_path_and_never_by_value(self) -> None:
        transport = SshInterface(
            host="10.0.0.1",
            username="ops",
            port=2222,
            password="hunter2",
            skip_host_verification=True,
        )

        line = transport.connect_provenance
        assert line == "ssh connect: ops@10.0.0.1:2222 auth=password via=direct"
        assert "hunter2" not in line

    def test_an_in_memory_key_names_no_file(self) -> None:
        transport = SshInterface(
            host="10.0.0.1",
            username="ops",
            private_key="-----BEGIN OPENSSH PRIVATE KEY-----",
            skip_host_verification=True,
        )

        line = transport.connect_provenance
        assert line == "ssh connect: ops@10.0.0.1:22 auth=key via=direct"
        assert "BEGIN OPENSSH" not in line

    def test_no_credential_at_all_is_reported_rather_than_hidden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        transport = SshInterface(host="10.0.0.1", username="ops", skip_host_verification=True)

        assert transport.auth_label == "none"
        assert "auth=none" in transport.connect_provenance

    def test_what_is_logged_is_what_is_dialed(self, tmp_path: Path) -> None:
        key_file = tmp_path / "id_ed25519"
        transport = SshInterface(
            host="10.0.0.1",
            username="ops",
            password="admin",
            private_key_path=key_file,
            skip_host_verification=True,
        )

        options = transport._connect_options()  # pyright: ignore[reportPrivateUsage]

        assert transport.auth_label == "key-file"
        assert options["client_keys"] == [str(key_file)]
        assert "password" not in options


class TestChainProvenance:
    """The via chain is rendered in full — an owned hop and a borrowed one alike."""

    def test_an_owned_via_is_the_hop(self) -> None:
        jump = SshInterface(
            host="bastion", username="ops", password="admin", skip_host_verification=True
        )
        target = SshInterface(
            host="dut",
            username="root",
            password="admin",
            via=jump,
            skip_host_verification=True,
        )

        assert target.hop is jump
        assert target.route_label == "bastion -> dut"

    def test_a_borrowed_tunnel_is_the_hop(self) -> None:
        jump = SshInterface(
            host="bastion", username="ops", password="admin", skip_host_verification=True
        )
        target = SshInterface(
            host="dut",
            username="root",
            password="admin",
            tunnel=jump,
            skip_host_verification=True,
        )

        assert target.hop is jump
        assert target.route_label == "bastion -> dut"

    def test_a_multi_hop_chain_is_rendered_whole_not_summarized(self) -> None:
        bastion = SshInterface(
            host="bastion", username="ops", password="admin", skip_host_verification=True
        )
        edge = SshInterface(
            host="edge",
            username="ops",
            password="admin",
            tunnel=bastion,
            skip_host_verification=True,
        )
        dut = SshInterface(
            host="dut",
            username="root",
            password="admin",
            tunnel=edge,
            skip_host_verification=True,
        )

        assert dut.connect_provenance == (
            "ssh connect: root@dut:22 auth=password via=bastion -> edge"
        )
        assert dut.route_label == "bastion -> edge -> dut"


class TestTopologyFromArtifacts:
    """The end the provenance lines exist for: no source-diving to read a run."""

    async def test_a_run_records_its_route_its_chain_and_its_auth_decision(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        jump_peer = await _LoopbackSshd.start(username="ops", password="jump")
        dut_peer = await _LoopbackSshd.start(username="root", password="admin")
        # Read before the listeners close: a closed acceptor reports port 0.
        jump_port = jump_peer.port
        dut_port = dut_peer.port
        opened: list[RemoteInterface] = []

        class Suite(LitmusTest):
            async def configure(self, utils: TestUtils) -> dict[str, object]:
                jump = await utils.use_shell(
                    host="127.0.0.1",
                    port=jump_peer.port,
                    username="ops",
                    password="jump",
                    skip_host_verification=True,
                )
                dut = await utils.use_shell(
                    host="127.0.0.1",
                    port=dut_peer.port,
                    username="root",
                    password="admin",
                    skip_host_verification=True,
                    via=jump,
                )
                opened.extend(shell.ssh for shell in (dut, jump) if shell.ssh is not None)
                return {}

            def build_suite(self) -> LitmusTest:
                return self.case("claim", self._claim)

            async def _claim(self, utils: TestUtils) -> Verdict:
                assert (await utils.exec("uname")).exit_code == 0
                return PASS

        try:
            with caplog.at_level("INFO", logger="litmus"):
                outcome = await Suite(name="dut").run()
        finally:
            for remote in opened:
                await remote.disconnect()
            await dut_peer.aclose()
            await jump_peer.aclose()

        assert outcome.state is State.PASS
        lines = [record.getMessage() for record in caplog.records]
        assert lines == [
            f"ssh connect: ops@127.0.0.1:{jump_port} auth=password via=direct",
            f"ssh connect: root@127.0.0.1:{dut_port} auth=password via=127.0.0.1",
            "exec routing: via 127.0.0.1 -> 127.0.0.1 (declared via use_shell)",
            "exec routing: via 127.0.0.1 -> 127.0.0.1 (inherited from parent)",
        ]
