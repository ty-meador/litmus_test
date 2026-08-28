"""Host-side execution choke point.

Every blocking or native call the framework makes on the test host enters through
`LocalExec`. Suite authors never hold an instance; `utils.exec` wraps `LocalExec.run`
once the test-class surface exists.

The instance is the unit of isolation. Nothing here is process-global, so a later
process-per-DUT worker can own its own `LocalExec` without a redesign.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import final


@final
@dataclass(frozen=True, slots=True)
class ExecResult:
    """What a host command produced.

    A non-zero `exit_code` is evidence, not an exception. `-1` means the process
    was killed by a signal, or was aborted before an exit status existed.
    """

    stdout: str
    stderr: str
    exit_code: int


@final
class LocalExec:
    """The framework-owned host executor.

    Contract:
      - `run` never raises for a command's own exit status. Failure is an
        `ExecResult` with that status.
      - A direct child killed by a signal, or aborted via `cancel_in_flight`,
        yields `exit_code == -1`.
      - `CancelledError` is never swallowed. The child is SIGTERM'd and reaped
        first, then the cancellation continues — a swallowed cancel here would
        strand the enclosing TaskGroup and leave a host process running.
      - Blocking callables enter only through `run_blocking` (`asyncio.to_thread`).
        Authors do not invoke this class; the framework does.
    """

    def __init__(self) -> None:
        # Mutable on purpose: in-flight children are the state this object exists to
        # own. Two instances do not share the set, which is what keeps process-per-DUT
        # an option rather than a rewrite.
        self._in_flight: set[asyncio.subprocess.Process] = set()

    async def run(self, command: str) -> ExecResult:
        """Run `command` on the test host through a shell.

        Never raises for the command's exit status. `CancelledError` still
        propagates, after the child process group has been signalled and reaped.
        """
        proc = await asyncio.create_subprocess_shell(
            command,
            # A host command must never block on the parent's stdin.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._in_flight.add(proc)
        try:
            try:
                stdout_b, stderr_b = await proc.communicate()
            except asyncio.CancelledError:
                await self._terminate(proc)
                raise
            return ExecResult(
                stdout=self._decode(stdout_b),
                stderr=self._decode(stderr_b),
                exit_code=self._exit_code(proc.returncode),
            )
        finally:
            self._in_flight.discard(proc)

    async def run_blocking[T](self, fn: Callable[[], T]) -> T:
        """Run a synchronous callable off the event loop.

        Vendor SDKs, heavy parsing, anything that would stall seventy pipelines if
        it touched the loop. Cancellation cannot kill the worker thread; work that
        must die with the run belongs in a subprocess (`run`) or a later
        process-per-DUT worker.
        """
        return await asyncio.to_thread(fn)

    async def cancel_in_flight(self) -> None:
        """SIGTERM every unreaped host child this instance started.

        Idempotent. Safe when nothing is running. In-flight `run` calls then
        settle as `ExecResult` with `exit_code == -1` rather than raising —
        abort is not a command failure.
        """
        for proc in tuple(self._in_flight):
            await self._terminate(proc)

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        # Always signal the group first. A cancelled `communicate()` can reap the
        # shell without touching its descendants; returning early would leak them.
        pid = proc.pid
        with suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGTERM)
        if proc.returncode is not None:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)
            # shielded: a second cancel here would orphan the child as a zombie
            await asyncio.shield(proc.wait())
        except asyncio.CancelledError:
            with suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)
            await asyncio.shield(proc.wait())
            raise

    @staticmethod
    def _decode(data: bytes | None) -> str:
        if data is None:
            return ""
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def _exit_code(returncode: int | None) -> int:
        if returncode is None or returncode < 0:
            return -1
        return returncode
