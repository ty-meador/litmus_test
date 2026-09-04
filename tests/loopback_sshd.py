"""In-process sshd for transport self-tests. Not a pytest test module."""

from __future__ import annotations

import asyncio

import asyncssh
import pytest


class LoopbackSshd:
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
        """The ephemeral port the in-process listener bound."""
        return self._listener.get_port()

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
    ) -> LoopbackSshd:
        """Listen on 127.0.0.1:0 with password and/or public-key auth."""
        host_key = LoopbackSshd.ed25519()
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
            process_factory=LoopbackSshd._handle,
            encoding="utf-8",
        )
        return cls(listener, username=username, password=password)

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
