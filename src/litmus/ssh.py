"""asyncssh-backed SSH transport.

Suite authors do not construct this. `ExecRouting.use_shell` / `get_shell` open
one beneath the scoped `TestUtils` methods. Self-tests import the module directly;
it is not on the public surface.

Via chains are a constructor argument: an upstream `SshInterface` is handed over
unconnected, brought up before this one, torn down after it, and hard-cancelled
with it. This interface owns its hop outright — a hop cannot be shared or
managed externally. Framework-owned `LitmusTest` via lifecycle (`via=` on the
Testable) sits on top of this: the via device owns the hop, and the consumer
borrows it through the routing layer's `tunnel=` channel.

`tunnel` is the mirror image, for the routing layer: a *borrowed* hop, handed
over connected and owned by whoever brought it up (the exec router). This
interface dials through it and never touches its lifecycle — no bring-up, no
teardown, no cancel. When the hop's owner hard-closes it, every connection
tunneled through it dies at the socket with it; the via-chain collapse of
PLAN §4.3 lives with the owner, not the borrower.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, final

import asyncssh
from asyncssh import Error as SshError
from asyncssh import SignalReceived

from litmus.executor import (
    DEFAULT_EXEC_OUTPUT_BUDGET,
    BoundedCapture,
    ExecResult,
    _validate_output_budget,  # pyright: ignore[reportPrivateUsage]
)
from litmus.remote import RemoteInterface

type AuthPath = Literal["agent", "key", "key-file", "password", "none"]


@final
@dataclass(frozen=True, slots=True)
class AuthChoice:
    """The credential path `connect` will take, and the exact credential it names.

    `source` is the file or socket that path reads — an agent socket, a key file. It
    is `None` where the path names no file (an in-memory key, a password) or where no
    credential exists at all. A password value never lands here: this object is
    rendered into the log artifact.
    """

    label: AuthPath
    source: str | None = None

    def __str__(self) -> str:
        """`key-file(/home/ops/id_ed25519)` — the path, and the credential it selected."""
        if self.source is None:
            return self.label
        return f"{self.label}({self.source})"


@final
class SshInterface(RemoteInterface):
    """SSH remote: connect, exec, hard-close, and jump-host tunnels.

    Contract beyond `RemoteInterface`:
      - `exec` never raises for a command's own exit status. A drop, abort, or
        signal-kill settles as `ExecResult` with `exit_code == -1`.
      - Stdout and stderr together are bounded by the same `output_budget`
        `LocalExec` uses. Exceeding it terminates that remote process (SIGTERM
        on the channel, then close) and settles with `truncated=True` plus the
        bytes actually received. The SSH session stays up.
      - `cancel_in_flight` aborts the asyncssh client (socket-level destroy),
        then walks the via chain with the same hard-close. Graceful hangup is
        not used on either path — SSH disconnect is a destroy, matching legacy.
      - A `via` hop is owned outright: it must arrive unconnected, `connect`
        rejects one that is already up, and every teardown path collapses the
        chain unconditionally.
      - A `tunnel` hop is borrowed: it must arrive connected, `connect`
        rejects one that is down, and no teardown path — `disconnect`,
        `cancel_in_flight`, or a failed `connect` — ever touches it.
      - `via` and `tunnel` are mutually exclusive: own the hop or borrow it,
        never both.
      - The address is fixed while the socket is live. `connect(host, port)`
        re-points the handle only while disconnected — after a firmware flash
        or DHCP churn the flow is `disconnect()`, reacquire, `connect(new_ip)`.
        The object in ctx stays valid. There is no self-healing: a socket that
        dies under the framework's abort stays dead (In-Flight Termination).
      - Auth resolution, in order: explicit `agent` path, in-memory
        `private_key`, `private_key_path`, `password`, then `SSH_AUTH_SOCK`.
        That order is declared once, in `_auth_choice`, and both the dialed
        options and the provenance line read it — what is logged is what was used.
      - `connect_provenance` names the endpoint, the credential path selected,
        and the full via chain. The credential's *value* is never rendered.
      - `skip_host_verification` is `known_hosts=None` (lab hosts without
        known_hosts entries). Default is to verify. Skipping writes a second
        provenance line naming the host, so an FT can see that verification
        was off.
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
        tunnel: SshInterface | None = None,
        output_budget: int = DEFAULT_EXEC_OUTPUT_BUDGET,
    ) -> None:
        """Create an unconnected SSH interface.

        Credentials are stored for connect time. `via`, when given, is an
        upstream hop this connection tunnels through; it must be handed over
        unconnected and belongs to this interface from then on. `tunnel`, when
        given, is a borrowed upstream hop; it must be handed over connected and
        its lifecycle stays with its owner.
        """
        if via is not None and tunnel is not None:
            msg = "via and tunnel are mutually exclusive: own the hop or borrow it, never both"
            raise ValueError(msg)
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
        self._tunnel = tunnel
        self._output_budget = _validate_output_budget(output_budget)
        self._connection: asyncssh.SSHClientConnection | None = None

    @property
    def output_budget(self) -> int:
        """Stdout+stderr combined ceiling, in bytes, matching `LocalExec`."""
        return self._output_budget

    @property
    def host(self) -> str:
        """Hostname or IP this interface connects to."""
        return self._host

    @property
    def port(self) -> int:
        """TCP port this interface connects to."""
        return self._port

    @property
    def address(self) -> tuple[str, int]:
        """`(host, port)` this interface dials. Re-pointed by `connect` while disconnected."""
        return (self._host, self._port)

    def _retarget(self, host: str, port: int) -> None:
        self._host = host
        self._port = port

    @property
    def auth_label(self) -> str:
        """Which credential path `connect` will use. For provenance logging."""
        return self._auth_choice().label

    @property
    def hop(self) -> SshInterface | None:
        """The upstream hop this connection dials through, owned (`via`) or borrowed."""
        if self._via is not None:
            return self._via
        return self._tunnel

    @property
    def connect_provenance(self) -> str:
        """The `ssh connect:` line: endpoint, credential path, and the full via chain.

        `via=direct` means this connection dials out from the test host itself. Any
        other value is the whole chain, test host first — the FT reconstructs the
        topology and the auth decision from this line without reading the suite.
        """
        hop = self.hop
        via = "direct" if hop is None else hop.route_label
        return (
            f"ssh connect: {self._username}@{self._host}:{self._port} "
            f"auth={self._auth_choice()} via={via}"
        )

    @property
    def provenance_lines(self) -> tuple[str, ...]:
        """Connect line, plus a loud skip when host-key verification is off."""
        lines = (self.connect_provenance,)
        if not self._skip_host_verification:
            return lines
        return (
            *lines,
            f"ssh host-key verification skipped: {self._host}:{self._port}",
        )

    async def _connect(self) -> None:
        via = self._via
        tunnel = self._tunnel
        if via is not None:
            if via.connected:
                msg = "via hop is owned by this interface and must be handed over unconnected"
                raise ConnectionError(msg)
            await via.connect()
        if tunnel is not None and not tunnel.connected:
            msg = "tunnel hop is borrowed and must be handed over connected"
            raise ConnectionError(msg)
        try:
            options = self._connect_options()
            if via is not None:
                options["tunnel"] = self._live_hop_connection(via, "via")
            elif tunnel is not None:
                options["tunnel"] = self._live_hop_connection(tunnel, "tunnel")
            self._connection = await asyncssh.connect(self._host, self._port, **options)
        except BaseException:
            # The owned via is collapsed on failure; the borrowed tunnel is
            # its owner's to close and is deliberately left up.
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
            process = await connection.create_process(
                command, encoding=None, stdin=asyncssh.DEVNULL
            )
        except SshError as error:
            # Socket died under us (abort, peer reset) before the channel
            # settled. exit_code -1, as legacy did for orphaned awaits, and
            # the cause named on stderr: a transport that dropped is not a
            # command that was killed, and the evidence has to tell them apart.
            return ExecResult.settled("", f"transport closed: {error}", None)
        capture = BoundedCapture(self._output_budget)

        async def stop() -> None:
            # Command-level halt. Close the channel so a full pipe cannot
            # deadlock the other stream; SIGTERM first so a real sshd can reap
            # the remote. Never abort the client — the session stays up.
            with contextlib.suppress(OSError, SshError):
                process.send_signal("TERM")
            with contextlib.suppress(Exception):
                process.close()

        try:
            try:
                await capture.drain(
                    process.stdout,
                    process.stderr,
                    on_truncate=stop,
                    treat_as_eof=(SshError, SignalReceived),
                )
                # Drain already has every byte we may keep. collect_output()
                # would join the rest of the receive window (up to ~2 MiB)
                # into one object — the leftover dump the budget exists to
                # prevent. Only slurp remainder on the non-truncated EOF path.
                if not capture.truncated:
                    leftover_out, leftover_err = process.collect_output()
                    if leftover_out:
                        out_chunk = (
                            leftover_out
                            if isinstance(leftover_out, bytes)
                            else leftover_out.encode("utf-8")
                        )
                        capture.accept(out_chunk, into=capture.stdout)
                    if leftover_err:
                        err_chunk = (
                            leftover_err
                            if isinstance(leftover_err, bytes)
                            else leftover_err.encode("utf-8")
                        )
                        capture.accept(err_chunk, into=capture.stderr)
                else:
                    await stop()
                await process.wait_closed()
            except asyncio.CancelledError:
                await stop()
                with contextlib.suppress(Exception):
                    await process.wait_closed()
                raise
            except SshError as error:
                return ExecResult.settled(
                    bytes(capture.stdout),
                    (
                        _name_transport_death(bytes(capture.stderr))
                        if capture.stderr
                        else f"transport closed: {error}"
                    ),
                    None,
                    truncated=capture.truncated,
                    captured_bytes=capture.captured_bytes,
                )
        finally:
            with contextlib.suppress(Exception):
                process.close()

        if process.exit_signal is None and process.exit_status is None:
            # The channel closed without ever reporting a status — the
            # connection went under the command (our hard close, or the peer's
            # reset). Same -1, same named cause; the remote's stderr, if any,
            # is kept beneath it.
            return ExecResult.settled(
                bytes(capture.stdout),
                _name_transport_death(bytes(capture.stderr)),
                None,
                truncated=capture.truncated,
                captured_bytes=capture.captured_bytes,
            )
        # A signal kill carries no usable exit status; settle it as the no-status case.
        exit_status = None if process.exit_signal is not None else process.exit_status
        return ExecResult.settled(
            bytes(capture.stdout),
            bytes(capture.stderr),
            exit_status,
            truncated=capture.truncated,
            captured_bytes=capture.captured_bytes,
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

    def _auth_choice(self) -> AuthChoice:
        """Resolve the credential path. The documented order lives here and nowhere else.

        Pure: it reads the stored credentials and parses nothing, so provenance can be
        rendered before — and without risking — a connect.
        """
        if self._agent is not None:
            return AuthChoice("agent", self._agent)
        if self._private_key is not None:
            return AuthChoice("key")
        if self._private_key_path is not None:
            return AuthChoice("key-file", str(self._private_key_path))
        if self._password is not None:
            return AuthChoice("password")
        sock = os.environ.get("SSH_AUTH_SOCK")
        if sock:
            return AuthChoice("agent", sock)
        return AuthChoice("none")

    def _auth_options(self) -> dict[str, object]:
        # Translation only — `_auth_choice` already decided. The credential re-reads
        # narrow what the choice guarantees; they cannot be false for their label.
        choice = self._auth_choice()
        if choice.label == "agent":
            return {"agent_path": choice.source, "client_keys": []}
        if choice.label == "key" and self._private_key is not None:
            key = asyncssh.import_private_key(self._private_key, passphrase=self._passphrase)
            return {"client_keys": [key], "agent_path": None}
        if choice.label == "key-file":
            return {
                "client_keys": [choice.source],
                "agent_path": None,
                "passphrase": self._passphrase,
            }
        if choice.label == "password" and self._password is not None:
            return {"password": self._password, "client_keys": [], "agent_path": None}
        raise ConnectionError(
            "SshInterface requires password, private_key, private_key_path, or SSH_AUTH_SOCK"
        )

    @staticmethod
    def _live_hop_connection(hop: SshInterface, role: str) -> asyncssh.SSHClientConnection:
        """The hop's open client connection; a connected hop without one is a lie."""
        connection = hop._connection
        if connection is None:
            msg = f"{role} hop reports connected without a live connection"
            raise ConnectionError(msg)
        return connection


def _name_transport_death(remote_stderr: str | bytes | None) -> str:
    """stderr for a command whose channel closed with no exit status.

    The framework's own name for the cause comes first; whatever the remote
    managed to write before the socket died is kept beneath it.
    """
    if isinstance(remote_stderr, bytes):
        remote_stderr = remote_stderr.decode("utf-8", errors="replace")
    tail = (remote_stderr or "").strip()
    return "transport closed" if not tail else f"transport closed\n{tail}"
