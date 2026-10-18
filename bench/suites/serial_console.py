"""A console reached by its tty node, the way a USB adapter is (``make bench-serial-up``).

What a developer wants to say: "this board's console answers on ``/dev/ttyUSB0``; prove
the prompt syncs, that a failing command's status is data, that a large reply arrives
whole, and that a wedged command can be cut." Four claims, in that order, because each
assumes the previous one.

What the framework gives for it: ``utils.use_serial(device=...)`` in ``configure()``,
beside ``use_shell``. The route is declared once, every claim reads the console through
``utils.exec``, and the port is the framework's to close: after the post-cleanup survey
on a normal run, and from the run-level abort sweep when an operator Ctrl-C lands. No
transport import, no handle in ctx, no ``cleanup()`` override (``bench/FRICTION.md``,
F2, closed by this shape).

The console on the far end is the self-test ``FakeConsole`` served by the surrogate
(``python -m bench.serial_pair up``): ``uname`` echoes, ``fail`` exits 1, ``dump N``
replies with N bytes, ``sleep N`` parks until Ctrl-C. A pty drops BREAK, so the cancel
lands on the Ctrl-C fallback and the port is closed afterwards, as the contract says.

Run it: ``SERIAL_DEVICE=/dev/pts/N PYTHONPATH=. litmus run bench/suites/serial_console.py``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict, evidence


class SerialConsole(LitmusTest):
    """Open a console by path and prove the exec contract end to end over the wire."""

    def __init__(
        self,
        *,
        name: str = "serial-console",
        baud: int = 115200,
        prompt_timeout_s: float = 5.0,
        dump_bytes: int = 4096,
        exec_latency_limit_ms: float = 1000.0,
    ) -> None:
        """Capture the line settings and the limits; the device path comes from the run.

        Args:
            name: Node name used by logs, artifacts, and failures.
            baud: Line rate requested at open; a pty ignores it, an adapter does not.
            prompt_timeout_s: How long one ``exec`` waits for the prompt to return.
            dump_bytes: Size of the large reply the ``large-reply`` claim asks for.
            exec_latency_limit_ms: Upper limit on one short round trip.
        """
        super().__init__(name=name)
        self._baud = baud
        self._prompt_timeout_s = prompt_timeout_s
        self._dump_bytes = dump_bytes
        self._exec_latency_limit_ms = exec_latency_limit_ms

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Route ``utils.exec`` to the tty ``SERIAL_DEVICE`` names; a dead port is a Fail."""
        device = utils.env.require("SERIAL_DEVICE")
        try:
            await utils.use_serial(
                device=device,
                baud=self._baud,
                login_timeout=self._prompt_timeout_s,
                prompt_timeout=self._prompt_timeout_s,
            )
        except (ConnectionError, OSError, ModuleNotFoundError) as error:
            # Any of these means the same thing to the run: no console to test.
            raise TestFailure(f"console did not open: {error}") from error
        return {
            "device": device,
            "baud": self._baud,
            "dump_bytes": self._dump_bytes,
            "exec_latency_limit_ms": self._exec_latency_limit_ms,
        }

    def build_suite(self) -> LitmusTest:
        """Prompt sync first; every later claim assumes the console answers."""
        return self.sequence(
            self.name,
            [
                self.test("prompt-sync", self._prompt_sync, fail_after=10.0),
                self.test("exit-status-is-data", self._exit_status, fail_after=10.0),
                self.test("large-reply", self._large_reply, fail_after=20.0),
                self.test("cancel-in-flight", self._cancel_in_flight, fail_after=20.0),
            ],
        )

    @evidence("stdout", "exit_code", "exec_latency_ms")
    async def _prompt_sync(self, utils: TestUtils) -> Verdict:
        """One short command comes back with its status, inside the latency limit."""
        started = time.monotonic()
        result = await utils.exec("uname")
        latency_ms = (time.monotonic() - started) * 1000.0
        reading = utils.measure(
            "exec_latency_ms",
            round(latency_ms, 1),
            unit="ms",
            limits=(None, utils.ctx["exec_latency_limit_ms"]),
        )
        stdout = result.stdout.strip()
        if result.exit_code == 0 and stdout == "uname" and reading.in_limits:
            return PASS(
                {"stdout": stdout, "exit_code": result.exit_code, "exec_latency_ms": reading.value}
            )
        raise TestFailure(
            "console did not answer a short command cleanly",
            "exit 0, 'uname' echoed, within limit",
            f"exit {result.exit_code}, {stdout!r}, {reading.value} ms",
        )

    @evidence("stdout", "exit_code")
    async def _exit_status(self, utils: TestUtils) -> Verdict:
        """A non-zero status is a reading, not an exception."""
        result = await utils.exec("fail")
        stdout = result.stdout.strip()
        if result.exit_code == 1 and stdout == "nope":
            return PASS({"stdout": stdout, "exit_code": result.exit_code})
        raise TestFailure(
            "failing command's status was not carried",
            "exit 1 with 'nope'",
            f"exit {result.exit_code} with {stdout!r}",
        )

    @evidence("received_bytes", "truncated")
    async def _large_reply(self, utils: TestUtils) -> Verdict:
        """A reply larger than one pty write arrives whole and untruncated."""
        wanted: int = utils.ctx["dump_bytes"]
        result = await utils.exec(f"dump {wanted}")
        received = len(result.stdout.strip())
        utils.measure("reply_bytes", received, unit="B", limits=(wanted, wanted))
        if result.exit_code == 0 and received == wanted and not result.truncated:
            return PASS({"received_bytes": received, "truncated": result.truncated})
        raise TestFailure(
            "large reply did not arrive whole",
            f"{wanted} bytes, exit 0, not truncated",
            f"{received} bytes, exit {result.exit_code}, truncated={result.truncated}",
        )

    @evidence("exit_code", "stderr", "port_closed")
    async def _cancel_in_flight(self, utils: TestUtils) -> Verdict:
        """A parked command is cut and the port is closed; the parked exec settles -1.

        The halt is the one the abort sweep runs; the suite reaches it through the
        active route (``use_serial()`` with no arguments is the read form), on the
        transport the framework opened, never one of its own.
        """
        console = (await utils.use_serial()).ssh
        if console is None:
            raise TestFailure("the exec route is not the console", "via the tty", "local")
        parked = asyncio.create_task(utils.exec("sleep 30"))
        await asyncio.sleep(0.5)
        await console.cancel_in_flight()
        result = await parked
        closed = not console.connected
        if result.exit_code == -1 and closed:
            return PASS(
                {
                    "exit_code": result.exit_code,
                    "stderr": result.stderr.strip(),
                    "port_closed": True,
                }
            )
        raise TestFailure(
            "cancel did not settle the parked command and close the port",
            "exit -1 and port closed",
            f"exit {result.exit_code}, connected={console.connected}",
        )
