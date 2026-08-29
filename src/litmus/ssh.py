"""asyncssh-backed SSH transport.

Suite authors do not construct this. `utils.get_shell` / `utils.use_shell` will
hand one over once that surface exists. Self-tests import the module directly.
Not on the public surface.

Via chains are a constructor argument: an upstream `SshInterface` is handed over
unconnected, brought up before this one, torn down after it, and hard-cancelled
with it. This interface owns its hop outright — a hop cannot be shared or
managed externally. Framework-owned `LitmusTest` via lifecycle (TestParams) is a
later layer on top of this.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import final

import asyncssh
from asyncssh import Error as SshError

from litmus.executor import ExecResult
from litmus.remote import RemoteInterface


@final
class SshInterface(RemoteInterface):
    """SSH remote: connect, exec, hard-close, and jump-host tunnels.

    Contract beyond `RemoteInterface`:
      - `exec` never raises for a command's own exit status. A drop, abort, or
        signal-kill settles as `ExecResult` with `exit_code == -1`.
      - `cancel_in_flight` aborts the asyncssh client (socket-level destroy),
        then walks the via chain with the same hard-close. Graceful hangup is
        not used on either path — SSH disconnect is a destroy, matching legacy.
      - A `via` hop is owned outright: it must arrive unconnected, `connect`
        rejects one that is already up, and every teardown path collapses the
        chain unconditionally.
      - Auth resolution, in order: explicit `agent` path, in-memory
        `private_key`, `private_key_path`, `password`, then `SSH_AUTH_SOCK`.
      - `skip_host_verification` is `known_hosts=None` (lab hosts without
        known_hosts entries). Default is to verify.
    """

    def __init__(
        self,
        *,
        host: str,
        username: str,
        port: int = 22,
        password: str | None = None,
        private_key: str | bytes | None = None,
        private_key_path: str | Path | None = None,
        passphrase: str | None = None,
        agent: str | None = None,
        skip_host_verification: bool = False,
        via: SshInterface | None = None,
    ) -> None:
        """Create an unconnected SSH interface.

        Credentials are stored for connect time. `via`, when given, is an
        upstream hop this connection tunnels through; it must be handed over
        unconnected and belongs to this interface from then on.
        """
        super().__init__()
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._private_key = private_key
        self._private_key_path = Path(private_key_path) if private_key_path is not None else None
        self._passphrase = passphrase
        self._agent = agent
        self._skip_host_verification = skip_host_verification
        self._via = via
        self._connection: asyncssh.SSHClientConnection | None = None

    @property
    def host(self) -> str:
        """Hostname or IP this interface connects to."""
        return self._host

    @property
    def port(self) -> int:
        """TCP port this interface connects to."""
        return self._port

    @property
    def auth_label(self) -> str:
        """Which credential path `connect` will use. For provenance logging."""
        if self._agent is not None:
            return "agent"
        if self._private_key is not None:
            return "key"
        if self._private_key_path is not None:
            return "key-file"
        if self._password is not None:
            return "password"
        if os.environ.get("SSH_AUTH_SOCK"):
            return "agent"
        return "none"

    async def reconnect_to(self, host: str, port: int | None = None) -> None:
        """Point this handle at a new address and replace the socket.

        Use after events that kill the SSH session but not the logical device
        (firmware flash, DHCP churn). The object in ctx stays valid.
        """
        self._host = host
        if port is not None:
            self._port = port
        await self.reconnect()

    async def _connect(self) -> None:
        via = self._via
        if via is not None:
            if via.connected:
                msg = "via hop is owned by this interface and must be handed over unconnected"
                raise ConnectionError(msg)
            await via.connect()
        try:
            options = self._connect_options()
            if via is not None:
                if via._connection is None:
                    msg = "via hop reports connected without a live connection"
                    raise ConnectionError(msg)
                options["tunnel"] = via._connection
            self._connection = await asyncssh.connect(self._host, self._port, **options)
        except BaseException:
            if via is not None:
                with contextlib.suppress(Exception):
                    await via.disconnect()
            raise

    async def _disconnect(self) -> None:
        await self._abort_client()
        if self._via is not None:
            await self._via.disconnect()

    async def _cancel_in_flight(self) -> None:
        await self._abort_client()
        if self._via is not None:
            await self._via.cancel_in_flight()

    async def _exec(self, command: str) -> ExecResult:
        connection = self._connection
        if connection is None:
            raise ConnectionError("remote interface is not connected")
        try:
            result = await connection.run(command, check=False)
        except SshError:
            # Socket died under us (abort, peer reset). Settle as no-status,
            # matching legacy: orphaned awaits resolve with exit_code -1.
            return ExecResult(stdout="", stderr="", exit_code=-1)
        return ExecResult(
            stdout=self._as_text(result.stdout),
            stderr=self._as_text(result.stderr),
            exit_code=self._exit_code(result.exit_status, result.exit_signal),
        )

    async def _abort_client(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        connection.abort()
        with contextlib.suppress(Exception):
            await connection.wait_closed()

    def _connect_options(self) -> dict[str, object]:
        options: dict[str, object] = {"username": self._username}
        if self._skip_host_verification:
            options["known_hosts"] = None
        options.update(self._auth_options())
        return options

    def _auth_options(self) -> dict[str, object]:
        if self._agent is not None:
            return {"agent_path": self._agent, "client_keys": []}
        if self._private_key is not None:
            key = asyncssh.import_private_key(self._private_key, passphrase=self._passphrase)
            return {"client_keys": [key], "agent_path": None}
        if self._private_key_path is not None:
            return {
                "client_keys": [str(self._private_key_path)],
                "agent_path": None,
                "passphrase": self._passphrase,
            }
        if self._password is not None:
            return {"password": self._password, "client_keys": [], "agent_path": None}
        sock = os.environ.get("SSH_AUTH_SOCK")
        if sock:
            return {"agent_path": sock, "client_keys": []}
        raise ConnectionError(
            "SshInterface requires password, private_key, private_key_path, or SSH_AUTH_SOCK"
        )

    @staticmethod
    def _as_text(data: str | bytes | None) -> str:
        if data is None:
            return ""
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return data

    @staticmethod
    def _exit_code(exit_status: int | None, exit_signal: object) -> int:
        if exit_signal is not None:
            return -1
        if exit_status is None or exit_status < 0:
            return -1
        return exit_status
