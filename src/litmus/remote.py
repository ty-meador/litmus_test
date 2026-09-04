"""The remote transport contract.

SSH, and later serial/UART, are peers behind this class. Protocol-specific
methods (CoAP get/put, HTTP post, SSH port-forward) belong on the subclass —
putting them here would freeze UART out of the port.

Suite authors do not construct a `RemoteInterface`. `ExecRouting.get_shell`
hands one to `TestUtils`, which returns the scoped `Shell`. Self-tests and
transport implementations import this module directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import final

from litmus.executor import ExecResult


class RemoteInterface(ABC):
    """Lifecycle and in-flight halt for every remote transport.

    Contract:
      - `connect` / `disconnect` own the connected flag. Subclasses implement
        `_connect` and `_disconnect`; they do not set the flag themselves.
      - `connect` is idempotent on a live transport. An addressed transport
        (one whose `address` is not `None`) accepts `host` / `port` on
        `connect` and re-points itself only while disconnected: asking a live
        transport for a different address is refused with `ConnectionError`,
        never silently replaced. The author disconnects first. A transport
        with no address rejects `host` / `port` with `TypeError`.
      - `_disconnect` must be safe when the transport was never opened.
      - `disconnect` clears the flag even if `_disconnect` raises, so a later
        `exec` cannot silently run against a half-dead transport.
      - `exec` never raises for a command's own exit status. Failure is an
        `ExecResult`. Calling `exec` while not connected raises
        `ConnectionError` — a protocol-level reject, not a verdict.
      - `cancel_in_flight` is the load-bearing halt. It is idempotent and safe
        when nothing is running. After it returns, `connected` is false and a
        later `exec` rejects. In-flight operations must fail because the
        resource is dead, not complete and keep mutating hardware.
      - Default `_cancel_in_flight` is `_disconnect`. SSH will override with a
        hard destroy so pending streams die at the socket, not at the next
        poll.
      - `endpoint`, `hop`, and `route_label` state where this transport sits in
        the topology; `connect_provenance` is the line the router logs when it
        comes up. The Floor Technician reconstructs the path to the DUT from
        those lines, so a transport that hides its hop hides the topology.
    """

    def __init__(self) -> None:
        # Mutable on purpose: connected-ness is the state this object exists to
        # track. The flag is private so a subclass cannot desync it from the
        # actual transport.
        self._connected: bool = False

    @property
    def connected(self) -> bool:
        """True if the transport is open and `exec` is allowed to proceed."""
        return self._connected

    @property
    def address(self) -> tuple[str, int] | None:
        """`(host, port)` this transport dials, or `None` for a transport with no address.

        A UART on a local device node has no address; an SSH peer does. `connect`
        reads this to decide whether `host` / `port` arguments mean anything here.
        """
        return None

    @property
    def hop(self) -> RemoteInterface | None:
        """The upstream transport this one dials through, or `None` for a direct link.

        Ownership is not the question here: an owned via hop and a borrowed tunnel
        answer alike. This is the topology edge a provenance chain walks.
        """
        return None

    @property
    def endpoint(self) -> str:
        """How this transport's peer is named in route labels and provenance chains.

        Falls back to the subclass's `host` where it exposes one, so a peer with no
        address of its own — a UART on a local device node — is still named.
        """
        host = getattr(self, "host", None)
        if isinstance(host, str) and host:
            return host
        return "remote"

    @property
    def route_label(self) -> str:
        """The full path from the test host to this peer: `bastion -> edge -> dut`.

        Rendered test-host-first and never summarized. Reconstructing topology from
        the artifact is the whole point of the line this feeds.
        """
        chain: list[str] = []
        transport: RemoteInterface | None = self
        while transport is not None:
            chain.append(transport.endpoint)
            transport = transport.hop
        chain.reverse()
        return " -> ".join(chain)

    @property
    def connect_provenance(self) -> str:
        """The evidence line the router logs when this transport comes up.

        Subclasses name their protocol and the credential path they selected; the
        default states the route alone.
        """
        return f"connect: {self.route_label}"

    @final
    async def connect(self, host: str | None = None, port: int | None = None) -> None:
        """Open the transport. Idempotent if already connected.

        `host` / `port`, on an addressed transport, name where to dial. While
        disconnected they re-point the transport before it opens; on a live
        transport the same address is a no-op and a different one is refused
        with `ConnectionError` — disconnect first. A transport with no
        `address` rejects them with `TypeError`.

        Raises whatever `_connect` raises when the peer cannot be reached. A
        refused dial leaves the transport closed and free for the next attempt.
        """
        target = self._resolve_target(host, port)
        current = self.address
        if self._connected:
            if target is None or target == current:
                return
            assert current is not None
            msg = (
                f"Attempted to connect {type(self).__name__} to {target[0]}:{target[1]} "
                f"while it already held an active connection to {current[0]}:{current[1]}"
            )
            raise ConnectionError(msg)
        if target is not None and target != current:
            self._retarget(*target)
        await self._connect()
        self._connected = True

    def _resolve_target(self, host: str | None, port: int | None) -> tuple[str, int] | None:
        """The `(host, port)` a `connect` call asks for, or `None` when it names neither."""
        if host is None and port is None:
            return None
        current = self.address
        if current is None:
            msg = f"{type(self).__name__} has no address to connect to; it takes no host or port"
            raise TypeError(msg)
        return (host if host is not None else current[0], port if port is not None else current[1])

    @final
    async def disconnect(self) -> None:
        """Close the transport and release its resources.

        Safe when the transport was never opened or is already closed.
        The connected flag is cleared even if `_disconnect` raises, so a
        later `exec` cannot silently succeed against a half-dead transport.
        """
        try:
            await self._disconnect()
        finally:
            self._connected = False

    @final
    async def reconnect(self) -> None:
        """Replace the underlying connection, keeping this object as the handle.

        Use after events that kill the transport but not the logical device
        (firmware flash, warm reset).
        """
        await self.disconnect()
        await self.connect()

    @final
    async def exec(self, command: str) -> ExecResult:
        """Run `command` on the remote peer.

        A non-zero exit is evidence, not an exception. Raises `ConnectionError`
        if the transport is closed — including after `cancel_in_flight`.
        """
        if not self._connected:
            raise ConnectionError("remote interface is not connected")
        return await self._exec(command)

    @final
    async def cancel_in_flight(self) -> None:
        """Kill every in-flight operation as hard as the protocol allows.

        Idempotent. Safe when nothing is running. The connected flag is cleared
        even if `_cancel_in_flight` raises, so a later `exec` cannot silently
        succeed against a half-dead transport.
        """
        if not self._connected:
            return
        try:
            await self._cancel_in_flight()
        finally:
            self._connected = False

    def _retarget(self, host: str, port: int) -> None:
        """Point an addressed transport at a new `(host, port)`.

        Called by `connect` only while disconnected, and only when the requested
        address differs from `address`. Transports that report an address must
        override this; the default refuses.
        """
        msg = f"{type(self).__name__} reports an address but cannot re-point it"
        raise TypeError(msg)

    @abstractmethod
    async def _connect(self) -> None:
        """Open the protocol-specific connection. Called by `connect`."""

    @abstractmethod
    async def _disconnect(self) -> None:
        """Close the protocol-specific connection. Called by `disconnect`.

        Must be safe when `_connect` never ran.
        """

    @abstractmethod
    async def _exec(self, command: str) -> ExecResult:
        """Run `command` on an already-open transport. Called by `exec`."""

    async def _cancel_in_flight(self) -> None:
        """Protocol-specific hard close. Default is `_disconnect`.

        Override when hanging up is not enough to fail pending operations
        (SSH: destroy the client so in-flight streams die at the socket).
        """
        await self._disconnect()
