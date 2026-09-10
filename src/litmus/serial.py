"""Serial/UART console transport, a `RemoteInterface` peer of SSH.

Suite authors construct this directly until a `use_shell` shape exists for a
device path (deferred on #91). Self-tests and the transport-contract suite
import the module directly. Not on the public surface.

Two placements, one contract: a local device node via `pyserial-asyncio`, or
the same byte stream through an owned SSH `via` hop. `pyserial-asyncio` is an
optional extra (`litmus[serial]`); importing this module does not import it,
and `import litmus` never imports this module. Via placement talks SSH only.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, final

from asyncssh import Error as SshError

from litmus.executor import (
    DEFAULT_EXEC_OUTPUT_BUDGET,
    BoundedCapture,
    ExecResult,
    _validate_output_budget,  # pyright: ignore[reportPrivateUsage]
)
from litmus.remote import RemoteInterface
from litmus.ssh import SshInterface

_DEFAULT_PROMPT: Final[str] = r"[#$%] $"
_RC_MARKER: Final[str] = "__LITMUS_RC__"
_READ_CHUNK: Final[int] = 65536
_BREAK_SECONDS: Final[float] = 0.25
# A prompt timeout is named "frozen" only when the wire has been silent for at
# least this share of the wait. Half the budget is long enough that a console
# still dripping output (a slow boot, a paging `dmesg`) is never called wedged,
# and short enough that a genuine hang after the echo still earns the name.
_FROZEN_SHARE_OF_WAIT: Final[float] = 0.5
_MISSING_SERIAL_EXTRA: Final[str] = (
    "SerialInterface local placement requires the 'serial' extra; "
    "install it with: pip install 'litmus[serial]'"
)

# CSI, OSC, and 2-byte ESC sequences. Applied to the accumulated decoded
# buffer so a colorized prompt split across two UART reads still resolves.
_ANSI = re.compile(
    r"\x1b(?:[@-Z\\-_]"
    r"|\[[0-?]*[ -/]*[@-~]"
    r"|][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|[P^_][^\x1b]*\x1b\\)"
)
_RC_LINE = re.compile(rf"^{re.escape(_RC_MARKER)}=(\d+)[ \t]*$", re.MULTILINE)


def _require_str(name: str, value: object) -> str:
    """Reject every non-str, including bytes."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str, not {type(value).__name__}")
    return value


def _require_non_empty_str(name: str, value: object) -> str:
    """Reject empty strings; the device path and prompt regex have no default that is blank."""
    text = _require_str(name, value)
    if not text:
        raise ValueError(f"{name} must be a non-empty str")
    return text


def _require_positive_int(name: str, value: object) -> int:
    """Reject bools (they subclass int) and every non-positive."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, not {type(value).__name__}")
    if value < 1:
        raise ValueError(f"{name} must be >= 1")
    return value


def _require_finite_seconds(name: str, value: object) -> float:
    """Reject bools, NaN/inf, and non-positive durations."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a finite number of seconds, not {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive finite number of seconds")
    return number


def _compile_prompt(name: str, value: object) -> re.Pattern[str]:
    """Compile a prompt regex; a bad pattern is a suite bug, not a verdict."""
    return re.compile(_require_non_empty_str(name, value))


def _posix_bridge_command(device: str, baud: int) -> str:
    """Remote command whose stdin/stdout is the UART, for via placement.

    `stty` plus a bidirectional `cat` copier so the hop box needs no extra
    packages. Self-tests override this with the loopback sshd's `console`.
    """
    quoted = shlex.quote(device)
    return (
        f"stty -F {quoted} {baud} raw -echo -echoe -echok clocal "
        f"&& exec 3<>{quoted} && cat <&3 & cat >&3"
    )


def _strip_ansi(text: str) -> str:
    """Drop complete VT100 sequences; an incomplete ESC at the tail stays."""
    return _ANSI.sub("", text)


def _normalize_newlines(text: str) -> str:
    r"""CRLF and bare CR become `\n`, matching every other transport's stdout."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _decode_for_match(raw: bytes) -> str:
    """UTF-8 with replacement, ANSI stripped, newlines normalized — then match."""
    decoded = raw.decode("utf-8", errors="replace")
    return _normalize_newlines(_strip_ansi(decoded))


def _ends_with_pattern(text: str, pattern: re.Pattern[str]) -> re.Match[str] | None:
    """The last match, and only if it sits at the end of `text`."""
    last: re.Match[str] | None = None
    for match in pattern.finditer(text):
        last = match
    if last is not None and last.end() == len(text):
        return last
    return None


def _strip_echo(decoded: str, sent: str) -> str:
    """Strip-if-present: one leading echoed line matching what we wrote.

    Runs before CRLF normalization, as the Serial Line Discipline Contract
    specifies. A console with echo already off is unaffected.
    """
    for ending in ("\r\n", "\n", "\r"):
        prefix = f"{sent}{ending}"
        if decoded.startswith(prefix):
            return decoded[len(prefix) :]
    return decoded


def _split_status(body: str) -> tuple[str, int | None]:
    """Pull `__LITMUS_RC__=<digits>` out of the reply. Missing → no status."""
    matches = list(_RC_LINE.finditer(body))
    if not matches:
        return body.rstrip("\n"), None
    last = matches[-1]
    stripped = f"{body[: last.start()]}{body[last.end() :]}".rstrip("\n")
    return stripped, int(last.group(1))


def _timeout_stderr(frozen: bool) -> str:
    """Name the timeout so an FT can tell wedged from slow without the suite."""
    if frozen:
        return "prompt timeout: wedged, frozen"
    return "prompt timeout: slow, still producing bytes"


def _import_serial_asyncio() -> Any:
    """Load the optional extra; the error names how to install it."""
    try:
        import serial_asyncio  # pyright: ignore[reportMissingTypeStubs]
    except ImportError as error:
        raise ModuleNotFoundError(_MISSING_SERIAL_EXTRA) from error
    return serial_asyncio


@dataclass(frozen=True, slots=True)
class _Read:
    """One prompt-wait: the decoded text plus how the wait ended."""

    text: str
    raw: bytes
    truncated: bool
    closed: bool
    timed_out: bool
    frozen: bool
    captured_bytes: int


@final
class SerialInterface(RemoteInterface):
    r"""Serial console: connect, exec, hard-close, and an owned SSH via hop.

    Contract beyond `RemoteInterface` (see `docs/hil/serial.md`):
      - `address` is `None`. `connect(host, port)` is a `TypeError`.
      - `exec` never raises for a command's own completion. A prompt timeout
        or a lost `__LITMUS_RC__` sentinel settles as `exit_code == -1` with
        the captured text kept as evidence.
      - DUT bytes land only in `stdout`. `stderr` is transport-generated
        (`"prompt timeout"`, `"transport closed"`).
      - Input is written as bare `\r`. Output is CRLF-normalized, one echoed
        line is stripped if present, and ANSI is stripped on the accumulated
        decoded buffer before the prompt regex runs.
      - Hitting `output_budget` interrupts the wire (BREAK, then Ctrl-C) and
        settles `truncated=True`; the port stays open.
      - `cancel_in_flight` is BREAK, then Ctrl-C, then an unconditional close.
        Through a via hop there is no BREAK: a byte bridge cannot carry a line
        condition, so the halt is Ctrl-C then closing the bridging channel.
        The hop itself is not cancelled — that would take down every other
        consumer of the SSH connection. `disconnect` still owns the hop's
        lifecycle.
      - A `via` hop is owned: it must arrive unconnected, `connect` brings it
        up, a failed connect collapses it, `disconnect` tears it down.
    """

    def __init__(
        self,
        *,
        device: str,
        baud: int = 115200,
        prompt: str = _DEFAULT_PROMPT,
        login_prompt: str | None = None,
        password_prompt: str | None = None,
        username: str | None = None,
        password: str | None = None,
        login_timeout: float = 10.0,
        prompt_timeout: float = 30.0,
        via: SshInterface | None = None,
        bridge_command: str | None = None,
        output_budget: int = DEFAULT_EXEC_OUTPUT_BUDGET,
    ) -> None:
        if via is not None and not isinstance(via, SshInterface):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "via must be an SshInterface; a console has no tunnel to borrow"
            raise TypeError(msg)
        super().__init__()
        self._device = _require_non_empty_str("device", device)
        self._baud = _require_positive_int("baud", baud)
        self._prompt = _compile_prompt("prompt", prompt)
        self._login_prompt = (
            None if login_prompt is None else _compile_prompt("login_prompt", login_prompt)
        )
        self._password_prompt = (
            None if password_prompt is None else _compile_prompt("password_prompt", password_prompt)
        )
        if self._login_prompt is not None:
            self._username = _require_non_empty_str("username", username)
        elif username is not None:
            self._username = _require_str("username", username)
        else:
            self._username = None
        if self._password_prompt is not None:
            self._password = _require_str("password", password if password is not None else "")
        else:
            self._password = password
        self._login_timeout = _require_finite_seconds("login_timeout", login_timeout)
        self._prompt_timeout = _require_finite_seconds("prompt_timeout", prompt_timeout)
        self._via = via
        self._bridge_command = (
            _require_non_empty_str("bridge_command", bridge_command)
            if bridge_command is not None
            else _posix_bridge_command(self._device, self._baud)
        )
        self._output_budget = _validate_output_budget(output_budget)
        self._reader: Any = None
        self._writer: Any = None
        self._serial: Any = None
        self._process: Any = None

    @property
    def output_budget(self) -> int:
        """Stdout+stderr combined ceiling, in bytes, matching `LocalExec`."""
        return self._output_budget

    @property
    def device(self) -> str:
        """Device node this interface opens, locally or on the via hop."""
        return self._device

    @property
    def baud(self) -> int:
        """Baud rate requested at construction."""
        return self._baud

    @property
    def prompt_timeout(self) -> float:
        """Seconds `exec` will wait for the prompt before settling `-1`."""
        return self._prompt_timeout

    @property
    def endpoint(self) -> str:
        """The device basename, so a route reads `bastion -> ttyUSB0`."""
        return Path(self._device).name

    @property
    def hop(self) -> SshInterface | None:
        """The owned SSH hop this console is reached through, if any."""
        return self._via

    @property
    def connect_provenance(self) -> str:
        """The `serial connect:` line: device, baud, and the via chain.

        `via=direct` means the device node is on the test host. A password
        used at login is never rendered.
        """
        hop = self.hop
        via = "direct" if hop is None else hop.route_label
        return f"serial connect: {self._device} baud={self._baud} via={via}"

    async def _connect(self) -> None:
        via = self._via
        if via is not None:
            if via.connected:
                msg = "via hop is owned by this interface and must be handed over unconnected"
                raise ConnectionError(msg)
            await via.connect()
        try:
            if via is not None:
                await self._open_via(via)
            else:
                await self._open_local()
            await self._resolve_login()
        except BaseException:
            await self._close_wire()
            if via is not None:
                with contextlib.suppress(Exception):
                    await via.disconnect()
            raise

    async def _disconnect(self) -> None:
        await self._close_wire()
        via = self._via
        if via is not None:
            await via.disconnect()

    async def _cancel_in_flight(self) -> None:
        # BREAK, Ctrl-C, close. The hop stays up: cancelling it would kill
        # every other consumer of that SSH connection (Exec Routing Contract).
        with contextlib.suppress(Exception):
            await self._send_break()
        with contextlib.suppress(Exception):
            await self._write_raw(b"\x03")
        await self._close_wire()

    async def _exec(self, command: str) -> ExecResult:
        sent = f'{command}; echo "{_RC_MARKER}=$?"'
        try:
            await self._write_enter(sent)
            outcome = await self._read_until(self._prompt, seconds=self._prompt_timeout)
        except asyncio.CancelledError:
            raise
        except (OSError, ConnectionError, SshError) as error:
            return ExecResult.settled("", f"transport closed: {error}", None)
        return self._settle_exec(sent, outcome)

    def _settle_exec(self, sent: str, outcome: _Read) -> ExecResult:
        """Turn a prompt-wait into an ExecResult: echo, sentinel, timeout, close."""
        decoded = outcome.raw.decode("utf-8", errors="replace")
        stripped = _strip_ansi(decoded)
        after_echo = _strip_echo(stripped, sent)
        text = _normalize_newlines(after_echo)
        if outcome.truncated:
            body, _status = _split_status(text)
            return ExecResult.settled(
                body,
                "",
                None,
                truncated=True,
                captured_bytes=outcome.captured_bytes,
            )
        if outcome.closed:
            body, _status = _split_status(text)
            return ExecResult.settled(body, "transport closed", None)
        if outcome.timed_out:
            body, _status = _split_status(text)
            return ExecResult.settled(body, _timeout_stderr(outcome.frozen), None)
        match = _ends_with_pattern(text, self._prompt)
        if match is None:
            body, status = _split_status(text)
            return ExecResult.settled(body, "", status)
        body, status = _split_status(text[: match.start()])
        return ExecResult.settled(body, "", status)

    async def _open_local(self) -> None:
        serial_asyncio = _import_serial_asyncio()
        reader, writer = await serial_asyncio.open_serial_connection(
            url=self._device,
            baudrate=self._baud,
            timeout=0,
            rtscts=False,
            dsrdtr=False,
        )
        self._reader = reader
        self._writer = writer
        transport = writer.transport
        self._serial = getattr(transport, "serial", None)

    async def _open_via(self, via: SshInterface) -> None:
        connection = SshInterface._live_hop_connection(via, "via")  # pyright: ignore[reportPrivateUsage]
        process = await connection.create_process(self._bridge_command, encoding=None)
        self._process = process
        self._reader = process.stdout
        self._writer = process.stdin

    async def _resolve_login(self) -> None:
        if self._login_prompt is not None:
            await self._expect(self._login_prompt, seconds=self._login_timeout, what="login prompt")
            await self._write_enter(self._username or "")
        if self._password_prompt is not None:
            await self._expect(
                self._password_prompt, seconds=self._login_timeout, what="password prompt"
            )
            await self._write_enter(self._password or "")
        await self._expect(self._prompt, seconds=self._login_timeout, what="shell prompt")

    async def _expect(self, pattern: re.Pattern[str], *, seconds: float, what: str) -> None:
        outcome = await self._read_until(pattern, seconds=seconds)
        if outcome.closed:
            raise ConnectionError(
                f"serial {what} never resolved on {self._device}: transport closed"
            )
        if outcome.timed_out or outcome.truncated:
            snippet = outcome.text[-200:]
            raise ConnectionError(
                f"serial {what} timed out on {self._device}; last bytes: {snippet!r}"
            )

    async def _read_until(self, pattern: re.Pattern[str], *, seconds: float) -> _Read:
        capture = BoundedCapture(self._output_budget)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        last_progress: float | None = None
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return self._timeout_read(
                    capture, frozen=self._is_frozen(loop.time(), last_progress, seconds)
                )
            try:
                chunk = await asyncio.wait_for(self._read_wire(), timeout=remaining)
            except TimeoutError:
                return self._timeout_read(
                    capture, frozen=self._is_frozen(loop.time(), last_progress, seconds)
                )
            if not chunk:
                return _Read(
                    text=_decode_for_match(bytes(capture.stdout)),
                    raw=bytes(capture.stdout),
                    truncated=capture.truncated,
                    closed=True,
                    timed_out=False,
                    frozen=False,
                    captured_bytes=capture.captured_bytes,
                )
            last_progress = loop.time()
            if not capture.accept(chunk, into=capture.stdout):
                await self._interrupt_wire()
                return _Read(
                    text=_decode_for_match(bytes(capture.stdout)),
                    raw=bytes(capture.stdout),
                    truncated=True,
                    closed=False,
                    timed_out=False,
                    frozen=False,
                    captured_bytes=capture.captured_bytes,
                )
            text = _decode_for_match(bytes(capture.stdout))
            if _ends_with_pattern(text, pattern) is not None:
                return _Read(
                    text=text,
                    raw=bytes(capture.stdout),
                    truncated=False,
                    closed=False,
                    timed_out=False,
                    frozen=False,
                    captured_bytes=capture.captured_bytes,
                )

    @staticmethod
    def _is_frozen(now: float, last_progress: float | None, seconds: float) -> bool:
        """True when the wire was silent for the final half of the wait — wedged, not slow.

        No bytes at all is the clearest hang. Otherwise the criterion is the
        silence before expiry measured against `_FROZEN_SHARE_OF_WAIT`, so the
        answer scales with the budget instead of a fixed number of milliseconds.
        """
        if last_progress is None:
            return True
        return (now - last_progress) >= seconds * _FROZEN_SHARE_OF_WAIT

    def _timeout_read(self, capture: BoundedCapture, *, frozen: bool) -> _Read:
        return _Read(
            text=_decode_for_match(bytes(capture.stdout)),
            raw=bytes(capture.stdout),
            truncated=capture.truncated,
            closed=False,
            timed_out=True,
            frozen=frozen,
            captured_bytes=capture.captured_bytes,
        )

    async def _read_wire(self) -> bytes:
        reader = self._reader
        if reader is None:
            return b""
        try:
            chunk = await reader.read(_READ_CHUNK)
        except OSError, ConnectionError, SshError:
            return b""
        if not chunk:
            return b""
        if isinstance(chunk, str):
            return chunk.encode("utf-8")
        return bytes(chunk)

    async def _write_enter(self, payload: str) -> None:
        await self._write_raw(payload.encode("utf-8") + b"\r")

    async def _write_raw(self, data: bytes) -> None:
        writer = self._writer
        if writer is None:
            raise ConnectionError("serial wire is closed")
        writer.write(data)
        drain = getattr(writer, "drain", None)
        if drain is not None:
            await drain()

    async def _send_break(self) -> None:
        serial = self._serial
        send_break = getattr(serial, "send_break", None)
        if send_break is None:
            return
        await asyncio.to_thread(send_break, _BREAK_SECONDS)

    async def _interrupt_wire(self) -> None:
        """BREAK then Ctrl-C, port stays open — the truncated-exec halt."""
        with contextlib.suppress(Exception):
            await self._send_break()
        with contextlib.suppress(Exception):
            await self._write_raw(b"\x03")

    async def _close_wire(self) -> None:
        process = self._process
        writer = self._writer
        self._process = None
        self._writer = None
        self._reader = None
        self._serial = None
        if process is not None:
            close = getattr(process, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    close()
            wait_closed = getattr(process, "wait_closed", None)
            if wait_closed is not None:
                with contextlib.suppress(Exception):
                    await wait_closed()
            return
        if writer is None:
            return
        with contextlib.suppress(Exception):
            writer.close()
        wait_closed = getattr(writer, "wait_closed", None)
        if wait_closed is not None:
            with contextlib.suppress(Exception):
                await wait_closed()
