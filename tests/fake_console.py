"""In-process login-and-shell console for SerialInterface self-tests.

`pty.openpty()` plus this script is the serial equivalent of LoopbackSshd:
real CRLF, real echo, a parked `sleep`, and the `__LITMUS_RC__=` sentinel
emitted by the console itself so the transport's parser is the thing under
test. The loopback sshd's `console` command attaches the same script to an
SSH channel for via-placement tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pty
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Final, final

SERIAL_BRIDGE_COMMAND: Final[str] = "console"
_RC_MARKER: Final[str] = "__LITMUS_RC__"
_SENTINEL_SUFFIX: Final[str] = f'; echo "{_RC_MARKER}=$?"'


type _ReadFn = Callable[[], Awaitable[bytes]]
type _WriteFn = Callable[[bytes], Awaitable[None]]


@final
class FakeConsole:
    """A POSIX-shaped console: prompt, echo, sentinel, park-on-sleep.

    Speaks CRLF. Echoes input unless `echo` is false. Answers `uname` by
    echoing the command (matching LoopbackSshd), `fail` with `nope` and
    status 1, and `sleep N` by parking until Ctrl-C or EOF.
    """

    def __init__(
        self,
        *,
        prompt: str = "$ ",
        echo: bool = True,
        emit_sentinel: bool = True,
        login_prompt: str | None = None,
        password_prompt: str | None = None,
        colorize_prompt: bool = False,
        hang: bool = False,
        slow: bool = False,
        emit_prompt: bool = True,
        mute: bool = False,
    ) -> None:
        self._prompt = prompt
        self._echo = echo
        self._emit_sentinel = emit_sentinel
        self._login_prompt = login_prompt
        self._password_prompt = password_prompt
        self._colorize_prompt = colorize_prompt
        self._hang = hang
        self._slow = slow
        self._emit_prompt = emit_prompt
        self._mute = mute
        self._stop = asyncio.Event()
        self._interrupt = asyncio.Event()

    def stop(self) -> None:
        """Unblock a parked session so the fixture can tear down."""
        self._stop.set()
        self._interrupt.set()

    def _encoded_prompt(self) -> bytes:
        prompt = self._prompt
        if self._colorize_prompt:
            prompt = f"\x1b[32m{prompt}\x1b[0m"
        return prompt.encode("utf-8")

    async def run_fd(self, fd: int) -> None:
        """Speak the console protocol on a pty master fd."""
        loop = asyncio.get_running_loop()
        incoming: asyncio.Queue[bytes] = asyncio.Queue()
        os.set_blocking(fd, False)

        def on_readable() -> None:
            try:
                data = os.read(fd, 4096)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            incoming.put_nowait(data)

        loop.add_reader(fd, on_readable)
        try:

            async def read() -> bytes:
                if self._stop.is_set():
                    return b""
                return await incoming.get()

            async def write(data: bytes) -> None:
                try:
                    os.write(fd, data)
                except OSError:
                    self._stop.set()
                    return

            await self._session(read, write)
        finally:
            with contextlib.suppress(Exception):
                loop.remove_reader(fd)

    async def run_ssh(self, process: Any, *, encoding: str | None) -> None:
        """Speak the console protocol on an asyncssh server process."""

        async def read() -> bytes:
            stdin = process.stdin
            if stdin is None:
                return b""
            try:
                chunk = await stdin.read(4096)
            except Exception:
                return b""
            if not chunk:
                return b""
            if isinstance(chunk, str):
                return chunk.encode("utf-8")
            return bytes(chunk)

        async def write(data: bytes) -> None:
            stdout = process.stdout
            if stdout is None:
                return
            if encoding is None:
                stdout.write(data)
            else:
                stdout.write(data.decode("utf-8", errors="replace"))
            drain = getattr(stdout, "drain", None)
            if drain is not None:
                await drain()

        try:
            await self._session(read, write)
        finally:
            with contextlib.suppress(Exception):
                process.exit(0)
            with contextlib.suppress(Exception):
                process.close()
            wait_closed = getattr(process, "wait_closed", None)
            if wait_closed is not None:
                with contextlib.suppress(Exception):
                    await wait_closed()

    async def _session(self, read: _ReadFn, write: _WriteFn) -> None:
        # pyserial flushes on open. Wait for the slave to be claimed before
        # writing the banner, otherwise connect sees an empty wire.
        await asyncio.sleep(0.1)
        if self._mute:
            # A dead line: reads everything, writes nothing, ever.
            while not self._stop.is_set():
                if not await read():
                    return
            return
        if self._login_prompt is not None:
            if self._emit_prompt:
                await write(self._login_prompt.encode("utf-8"))
            state = "login"
        elif self._emit_prompt:
            await write(self._encoded_prompt())
            state = "shell"
        else:
            state = "shell"
        # Bytes are pumped off the wire by their own task so a Ctrl-C lands while
        # a command is parked; the session coroutine is the one that is parked.
        lines: asyncio.Queue[str | None] = asyncio.Queue()
        pump = asyncio.create_task(self._pump(read, lines), name="fake-console-pump")
        try:
            while not self._stop.is_set():
                line = await lines.get()
                if line is None:
                    return
                state = await self._handle_line(line, write, state)
                if state == "done":
                    return
        finally:
            if not pump.done():
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump

    async def _pump(self, read: _ReadFn, lines: asyncio.Queue[str | None]) -> None:
        buf = bytearray()
        while not self._stop.is_set():
            chunk = await read()
            if not chunk:
                lines.put_nowait(None)
                return
            for byte in chunk:
                if byte == 0x03:
                    self._interrupt.set()
                    buf.clear()
                    continue
                if byte in (0x0D, 0x0A):
                    if byte == 0x0A and not buf:
                        continue
                    # Enter starts a command with a clean slate: a Ctrl-C from
                    # here on is for this command.
                    self._interrupt.clear()
                    lines.put_nowait(buf.decode("utf-8", errors="replace"))
                    buf.clear()
                    continue
                buf.append(byte)
        lines.put_nowait(None)

    async def _handle_line(self, line: str, write: _WriteFn, state: str) -> str:
        if state == "login":
            if self._echo:
                await write(f"{line}\r\n".encode())
            if not line:
                # A getty re-prompts on an empty name.
                await write(self._login_prompt.encode("utf-8"))  # type: ignore[union-attr]
                return "login"
            if self._password_prompt is not None:
                await write(self._password_prompt.encode("utf-8"))
                return "password"
            await write(self._encoded_prompt())
            return "shell"
        if state == "password":
            await write(b"\r\n")
            await write(self._encoded_prompt())
            return "shell"
        command, wants_sentinel = _split_command(line)
        if self._echo:
            await write(f"{line}\r\n".encode())
        if not command:
            # Enter on an empty line: a shell re-prompts, wedged or not.
            await write(self._encoded_prompt())
            return "shell"
        if self._hang:
            await self._park()
            return "shell"
        if self._slow:
            await self._drip(write)
            return "shell"
        exit_code = 0
        output = command
        if command == "fail":
            output = "nope"
            exit_code = 1
        elif command.startswith("sleep "):
            await self._park()
            if self._stop.is_set() or self._interrupt.is_set():
                return "shell"
            output = "woke"
        elif command.startswith("dump "):
            remaining = int(command.split()[1])
            output = "x" * remaining
        elif command == "flood":
            await self._flood(write)
            return "shell"
        if output:
            await write(f"{output}\r\n".encode())
        if wants_sentinel and self._emit_sentinel:
            await write(f"{_RC_MARKER}={exit_code}\r\n".encode())
        await write(self._encoded_prompt())
        return "shell"

    async def _park(self) -> None:
        stop_task = asyncio.create_task(self._stop.wait())
        interrupt_task = asyncio.create_task(self._interrupt.wait())
        try:
            done, pending = await asyncio.wait(
                {stop_task, interrupt_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                await task
        finally:
            for task in (stop_task, interrupt_task):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    async def _drip(self, write: _WriteFn) -> None:
        while not self._stop.is_set() and not self._interrupt.is_set():
            await write(b".")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.05)
                return
            except TimeoutError:
                continue

    async def _flood(self, write: _WriteFn) -> None:
        chunk = b"x" * 1024
        while not self._stop.is_set() and not self._interrupt.is_set():
            await write(chunk)
            await asyncio.sleep(0)


def _split_command(line: str) -> tuple[str, bool]:
    if _SENTINEL_SUFFIX in line:
        command, _sep, _rest = line.partition(_SENTINEL_SUFFIX)
        return command, True
    return line, False


@asynccontextmanager
async def open_pty_console(**kwargs: Any) -> AsyncGenerator[tuple[str, FakeConsole]]:
    """Yield `(slave_path, console)` with the console running on the pty master."""
    master_fd, slave_fd = pty.openpty()
    device = os.ttyname(slave_fd)
    os.close(slave_fd)
    console = FakeConsole(**kwargs)
    task = asyncio.create_task(console.run_fd(master_fd), name="fake-serial-console")
    try:
        yield device, console
    finally:
        console.stop()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(OSError):
            os.close(master_fd)
