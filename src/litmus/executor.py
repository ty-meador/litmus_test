"""Host-side execution choke point.

Every blocking or native call the framework makes on the test host enters through
`LocalExec`. Suite authors never hold an instance; each run seed gives `TestUtils`
one executor, shared by its per-node routed views.

The instance is the unit of isolation. Nothing here is process-global, so a later
process-per-DUT worker can own its own `LocalExec` without a redesign.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol, final

# One named ceiling for stdout+stderr combined, applied identically by LocalExec
# and SshInterface. 4 MiB keeps a kernel log; 70 DUTs at the cap stay under ~280 MiB.
DEFAULT_EXEC_OUTPUT_BUDGET = 4 * 1024 * 1024
_READ_CHUNK = 65536


class _ChunkReader(Protocol):
    async def read(self, n: int = -1) -> bytes | str: ...


def _validate_output_budget(output_budget: int) -> int:
    if output_budget < 1:
        raise ValueError("output_budget must be at least 1 byte; pass a positive byte ceiling")
    return output_budget


def _raw_size(data: str | bytes | None) -> int:
    if data is None:
        return 0
    if isinstance(data, bytes):
        return len(data)
    return len(data.encode("utf-8"))


@final
class BoundedCapture:
    """Accumulate stdout/stderr up to one shared byte budget.

    Shared by `LocalExec` and `SshInterface` so one named ceiling cannot drift.
    Not on the public package surface.
    """

    __slots__ = ("_budget", "captured_bytes", "stderr", "stdout", "truncated")

    def __init__(self, budget: int) -> None:
        self._budget = budget
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.captured_bytes = 0
        self.truncated = False

    def accept(self, chunk: bytes, *, into: bytearray) -> bool:
        """Append `chunk` up to the remaining budget. Returns False once truncated."""
        if not chunk:
            return not self.truncated
        if self.truncated:
            return False
        room = self._budget - self.captured_bytes
        if room <= 0:
            self.truncated = True
            return False
        if len(chunk) > room:
            into.extend(chunk[:room])
            self.captured_bytes += room
            self.truncated = True
            return False
        into.extend(chunk)
        self.captured_bytes += len(chunk)
        return True

    async def drain(
        self,
        stdout: _ChunkReader | None,
        stderr: _ChunkReader | None,
        *,
        on_truncate: Callable[[], Awaitable[None]] | None = None,
        treat_as_eof: tuple[type[BaseException], ...] = (),
    ) -> None:
        """Read both streams until EOF or the budget. Truncation calls `on_truncate`."""
        await asyncio.gather(
            self._pump(stdout, self.stdout, on_truncate, treat_as_eof),
            self._pump(stderr, self.stderr, on_truncate, treat_as_eof),
        )

    async def _pump(
        self,
        stream: _ChunkReader | None,
        buf: bytearray,
        on_truncate: Callable[[], Awaitable[None]] | None,
        treat_as_eof: tuple[type[BaseException], ...],
    ) -> None:
        if stream is None:
            return
        while not self.truncated:
            try:
                chunk = await stream.read(_READ_CHUNK)
            except treat_as_eof:
                return
            if not chunk:
                return
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            if self.accept(chunk, into=buf):
                continue
            if on_truncate is not None:
                await on_truncate()
            return


@final
@dataclass(frozen=True, slots=True)
class ExecResult:
    """What a host command produced.

    A non-zero `exit_code` is evidence, not an exception. `-1` means the process
    was killed by a signal, or was aborted before an exit status existed.
    `truncated` means stdout+stderr together hit the output budget; `captured_bytes`
    is how much was kept. Existing callers that omit both still construct a result.
    """

    stdout: str
    stderr: str
    exit_code: int
    truncated: bool = False
    captured_bytes: int = 0

    def __post_init__(self) -> None:
        # Default 0 means "not supplied": derive from the settled text so
        # ExecResult(stdout, stderr, exit_code) keeps working and compares equal
        # to settled() of the same strings. An explicit 0 on empty output stays 0.
        if self.captured_bytes == 0 and (self.stdout or self.stderr):
            object.__setattr__(
                self,
                "captured_bytes",
                len(self.stdout.encode("utf-8")) + len(self.stderr.encode("utf-8")),
            )

    @classmethod
    def settled(
        cls,
        stdout: str | bytes | None,
        stderr: str | bytes | None,
        exit_status: int | None,
        *,
        truncated: bool = False,
        captured_bytes: int | None = None,
    ) -> ExecResult:
        """Settle raw transport output into a result.

        Output arrives as whatever the transport produced — `bytes` (decoded UTF-8
        with replacement), `str`, or nothing (empty). An `exit_status` of `None` or
        below zero means no real status exists — signal kill, or abort before an exit
        status — and settles as `-1`. Every transport builds its results here, so the
        `-1` contract cannot drift between them. Truncation is evidence, not an
        exception: the marker and byte count ride on the same result.
        """
        counted = captured_bytes
        if counted is None:
            counted = _raw_size(stdout) + _raw_size(stderr)
        return cls(
            stdout=cls._decode(stdout),
            stderr=cls._decode(stderr),
            exit_code=-1 if exit_status is None or exit_status < 0 else exit_status,
            truncated=truncated,
            captured_bytes=counted,
        )

    @staticmethod
    def _decode(data: str | bytes | None) -> str:
        if data is None:
            return ""
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return data


@final
class LocalExec:
    """The framework-owned host executor.

    Contract:
      - `run` never raises for a command's own exit status. Failure is an
        `ExecResult` with that status.
      - A direct child killed by a signal, or aborted via `cancel_in_flight`,
        yields `exit_code == -1`.
      - Stdout and stderr together are bounded by `output_budget`. Exceeding it
        SIGTERMs the process group the same way abort does and settles with
        `truncated=True` plus the bytes actually kept. Truncation is not an
        exception.
      - Streaming reads preserve bytes already received on abort, budget kill,
        or a mid-command interrupt; decode happens only at settlement.
      - `CancelledError` is never swallowed. The child is SIGTERM'd and reaped
        first, then the cancellation continues — a swallowed cancel here would
        strand the enclosing TaskGroup and leave a host process running.
      - Blocking callables enter only through `run_blocking` (`asyncio.to_thread`).
        Authors do not invoke this class; the framework does.
    """

    def __init__(self, *, output_budget: int = DEFAULT_EXEC_OUTPUT_BUDGET) -> None:
        # Mutable on purpose: in-flight children are the state this object exists to
        # own. Two instances do not share the set, which is what keeps process-per-DUT
        # an option rather than a rewrite. The budget is per instance for the same
        # reason — not a process-global.
        self._output_budget = _validate_output_budget(output_budget)
        self._in_flight: set[asyncio.subprocess.Process] = set()

    @property
    def output_budget(self) -> int:
        """Stdout+stderr combined ceiling, in bytes, for every `run` on this instance."""
        return self._output_budget

    async def run(self, command: str) -> ExecResult:
        """Run `command` on the test host through a shell.

        Never raises for the command's exit status. Hitting the output budget
        terminates the child and settles a truncated result. `CancelledError`
        still propagates, after the child process group has been signalled and
        reaped.
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
        capture = BoundedCapture(self._output_budget)

        async def stop() -> None:
            await self._terminate(proc)

        try:
            try:
                await capture.drain(
                    proc.stdout,
                    proc.stderr,
                    on_truncate=stop,
                )
                # Pipe EOF is not process exit. communicate() reaped; drain does
                # not. A child that closed its pipes and then exited 0 would
                # otherwise settle as abort (-1) and vanish from _in_flight.
                if capture.truncated:
                    if proc.returncode is None:
                        await self._terminate(proc)
                else:
                    await proc.wait()
                return ExecResult.settled(
                    bytes(capture.stdout),
                    bytes(capture.stderr),
                    proc.returncode,
                    truncated=capture.truncated,
                    captured_bytes=capture.captured_bytes,
                )
            except asyncio.CancelledError:
                await self._terminate(proc)
                raise
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
        # Always signal the group first. A cancelled stream read can reap the
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
