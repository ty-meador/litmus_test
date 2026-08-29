"""In-process RemoteInterface for self-tests. No SSH.

Suite authors do not construct this. Self-tests and the later rack simulation
import the module directly. Not on the public surface.
"""

from __future__ import annotations

import asyncio
from typing import final

from litmus.executor import ExecResult
from litmus.remote import RemoteInterface


@final
class MockTransport(RemoteInterface):
    """A scriptable transport so lifecycle tests never need a real peer.

    Contract beyond `RemoteInterface`:
      - `script` registers an `ExecResult` for a command string.
      - Unscripted commands echo the command as stdout with exit 0.
      - `park` makes `exec` of that command wait until `cancel_in_flight`
        (or disconnect) fails it. That is the assertion surface for halt.
      - `commands`, `connect_count`, `disconnect_count`, `cancel_count`
        record what happened so a test can assert without a live sshd.
    """

    def __init__(self) -> None:
        super().__init__()
        # Mutable on purpose: this object *is* the test double. Counts, scripts,
        # and parked futures are the state self-tests inspect.
        self._results: dict[str, ExecResult] = {}
        self._parked: set[str] = set()
        self._commands: list[str] = []
        self._in_flight: set[asyncio.Future[ExecResult]] = set()
        self._connect_error: BaseException | None = None
        self._connect_count = 0
        self._disconnect_count = 0
        self._cancel_count = 0

    def script(self, command: str, result: ExecResult) -> None:
        """Make `exec(command)` return `result` once the transport is open."""
        self._results[command] = result

    def park(self, command: str) -> None:
        """Hold `exec(command)` until the transport is cancelled or closed."""
        self._parked.add(command)

    def fail_next_connect(self, error: BaseException) -> None:
        """Raise `error` from the next `connect`, then recover."""
        self._connect_error = error

    @property
    def commands(self) -> tuple[str, ...]:
        """Every command `exec` has been asked to run, in order."""
        return tuple(self._commands)

    @property
    def connect_count(self) -> int:
        """How many times `_connect` ran, including failed attempts."""
        return self._connect_count

    @property
    def disconnect_count(self) -> int:
        """How many times `_disconnect` ran, including via cancel."""
        return self._disconnect_count

    @property
    def cancel_count(self) -> int:
        """How many times `cancel_in_flight` actually tore the transport down."""
        return self._cancel_count

    @property
    def has_in_flight(self) -> bool:
        """True if a parked `exec` is waiting on this transport."""
        return bool(self._in_flight)

    @property
    def in_flight_count(self) -> int:
        """How many parked `exec` calls are waiting on this transport."""
        return len(self._in_flight)

    async def _connect(self) -> None:
        self._connect_count += 1
        if self._connect_error is not None:
            error = self._connect_error
            self._connect_error = None
            raise error

    async def _disconnect(self) -> None:
        self._disconnect_count += 1
        self._fail_in_flight("transport closed")

    async def _exec(self, command: str) -> ExecResult:
        self._commands.append(command)
        if command in self._parked:
            in_flight: asyncio.Future[ExecResult] = asyncio.get_running_loop().create_future()
            self._in_flight.add(in_flight)
            try:
                return await in_flight
            finally:
                self._in_flight.discard(in_flight)
        return self._results.get(command, ExecResult(stdout=command, stderr="", exit_code=0))

    async def _cancel_in_flight(self) -> None:
        self._cancel_count += 1
        await self._disconnect()

    def _fail_in_flight(self, message: str) -> None:
        for in_flight in tuple(self._in_flight):
            if not in_flight.done():
                in_flight.set_exception(ConnectionError(message))
