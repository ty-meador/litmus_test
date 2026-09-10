"""In-process sshd for transport self-tests. Not a pytest test module."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import asyncssh
import pytest
from tests.fake_console import SERIAL_BRIDGE_COMMAND, FakeConsole


class LoopbackSshd:
    """In-process sshd so self-tests never need a machine's sshd."""

    def __init__(
        self,
        listener: asyncssh.SSHAcceptor,
        *,
        username: str,
        password: str | None,
        accepted: list[asyncssh.SSHServerConnection],
    ) -> None:
        self._listener = listener
        self.username = username
        self.password = password
        self._accepted = accepted

    @property
    def connections(self) -> int:
        """How many client connections this listener has accepted so far."""
        return len(self._accepted)

    @property
    def port(self) -> int:
        """The ephemeral port the in-process listener bound."""
        return self._listener.get_port()

    def abort_connections(self) -> None:
        """Hard-drop every accepted client. A peer reset, not a graceful hangup."""
        for connection in tuple(self._accepted):
            connection.abort()

    def close_connections(self) -> None:
        """Graceful close of every accepted client. EOF, not a reset."""
        for connection in tuple(self._accepted):
            connection.close()

    async def aclose(self) -> None:
        """Stop the listener and wait until acceptors are gone."""
        self._listener.close()
        await self._listener.wait_closed()

    @classmethod
    async def start(
        cls,
        *,
        username: str = "root",
        password: str | None = "admin",
        authorized_key: asyncssh.SSHKey | None = None,
        encoding: str | None = "utf-8",
        drop_on_connect: bool = False,
    ) -> LoopbackSshd:
        """Listen on 127.0.0.1:0 with password and/or public-key auth.

        `encoding=None` puts the process factory in bytes mode so a test can
        emit binary or non-UTF-8 output. `drop_on_connect` aborts the accepted
        socket during the handshake.
        """
        host_key = LoopbackSshd.ed25519()
        expected_user = username
        expected_password = password
        authorized = authorized_key
        accepted: list[asyncssh.SSHServerConnection] = []
        drop = drop_on_connect

        class _Auth(asyncssh.SSHServer):
            def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
                accepted.append(conn)
                if drop:
                    conn.abort()

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

        async def process_factory(process: asyncssh.SSHServerProcess[Any]) -> None:
            await LoopbackSshd._handle(process, encoding=encoding)

        listener = await asyncssh.listen(
            "127.0.0.1",
            0,
            server_host_keys=[host_key],
            server_factory=_Auth,
            process_factory=process_factory,
            encoding=encoding,
        )
        return cls(listener, username=username, password=password, accepted=accepted)

    @staticmethod
    def ed25519() -> asyncssh.SSHKey:
        """A fresh host or client key for a test-owned listener."""
        return asyncssh.generate_private_key("ssh-ed25519")  # pyright: ignore[reportUnknownMemberType]

    @staticmethod
    def parse_agent(script: str) -> tuple[str, int]:
        """Read SSH_AUTH_SOCK and SSH_AGENT_PID from `ssh-agent -s` output."""
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
    def _write(stream: asyncssh.SSHWriter[Any], data: str | bytes, *, encoding: str | None) -> None:
        if encoding is None:
            stream.write(data if isinstance(data, bytes) else data.encode("utf-8"))
            return
        stream.write(data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data)

    @staticmethod
    async def _handle(process: asyncssh.SSHServerProcess[Any], *, encoding: str | None) -> None:
        raw = process.command or ""
        command = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if command == "fail":
            LoopbackSshd._write(process.stderr, "nope", encoding=encoding)
            process.exit(1)
        elif command.startswith("sleep "):
            try:
                await asyncio.sleep(float(command.split()[1]))
                LoopbackSshd._write(process.stdout, "woke", encoding=encoding)
                process.exit(0)
            except asyncio.CancelledError:
                process.exit(255)
                raise
        elif command.startswith("dump "):
            remaining = int(command.split()[1])
            await LoopbackSshd._write_chunks(
                process, process.stdout, "x", remaining, encoding=encoding
            )
        elif command.startswith("dump-err "):
            remaining = int(command.split()[1])
            await LoopbackSshd._write_chunks(
                process, process.stderr, "y", remaining, encoding=encoding
            )
        elif command.startswith("say-sleep "):
            _prefix, message, seconds = command.split(" ", 2)
            del _prefix
            try:
                LoopbackSshd._write(process.stdout, message, encoding=encoding)
                await asyncio.sleep(float(seconds))
                process.exit(0)
            except asyncio.CancelledError:
                process.exit(255)
                raise
            except Exception:
                with contextlib.suppress(Exception):
                    process.exit(255)
        elif command == "flood":
            await LoopbackSshd._write_chunks(process, process.stdout, "x", None, encoding=encoding)
        elif command == "stall":
            try:
                await asyncio.get_running_loop().create_future()
            except asyncio.CancelledError:
                process.exit(255)
                raise
        elif command == "binary":
            LoopbackSshd._write(process.stdout, b"\x00hello\x01world", encoding=encoding)
            process.exit(0)
        elif command == "non-utf8":
            LoopbackSshd._write(process.stdout, b"pre\xffpost", encoding=encoding)
            process.exit(0)
        elif command == SERIAL_BRIDGE_COMMAND or command.startswith(f"{SERIAL_BRIDGE_COMMAND} "):
            await FakeConsole().run_ssh(process, encoding=encoding)
            return
        else:
            LoopbackSshd._write(process.stdout, command, encoding=encoding)
            process.exit(0)
        process.close()
        await process.wait_closed()

    @staticmethod
    async def _write_chunks(
        process: asyncssh.SSHServerProcess[Any],
        stream: asyncssh.SSHWriter[Any],
        token: str,
        remaining: int | None,
        *,
        encoding: str | None,
    ) -> None:
        """Write `token` forever, or `remaining` times, then exit.

        A client that hits its output budget closes the channel mid-write; that
        must not take the SSH connection down with an unhandled exception.
        """
        try:
            while remaining is None or remaining > 0:
                take = 1024 if remaining is None else min(remaining, 1024)
                LoopbackSshd._write(stream, token * take, encoding=encoding)
                if remaining is not None:
                    remaining -= take
                await asyncio.sleep(0)
            process.exit(0)
        except asyncio.CancelledError:
            process.exit(255)
            raise
        except Exception:
            with contextlib.suppress(Exception):
                process.exit(255)
