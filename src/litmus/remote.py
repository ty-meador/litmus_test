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

    @final
    async def connect(self) -> None:
        """Open the transport. Idempotent if already connected.

        Raises whatever `_connect` raises when the peer cannot be reached.
        """
        if self._connected:
            return
        await self._connect()
        self._connected = True

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
