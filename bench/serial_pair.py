"""Serial surrogate: a socat null-modem pair with the self-test console on the far end.

``tests/fake_console.py`` already proves the parser on a ``pty.openpty()`` master.
What it cannot prove is the path a USB adapter takes: ``SerialInterface`` opening a
tty node *by path* through pyserial, configuring the line, and closing it. ``socat``
links two ptys; the console speaks on one, ``SerialInterface`` opens the other by its
``/dev/pts/N`` path. Same console, same sentinel, one more layer of truth.

A BREAK written into a pty is dropped by the kernel, so the in-flight cancel here
lands on the Ctrl-C fallback. That is the documented via-hop behaviour; it is
recorded in the verdict line, not papered over. Needs ``socat``; no root.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tests.fake_console import FakeConsole

from bench._common import report, require_tool
from litmus.serial import SerialInterface


async def _exercise(device: str) -> int:
    transport = SerialInterface(device=device, login_timeout=5.0, prompt_timeout=5.0)
    await transport.connect()
    print(transport.connect_provenance)
    failures = 0

    result = await transport.exec("uname")
    failures += report(
        "exec settles through a real tty node",
        result.exit_code == 0 and result.stdout.strip() == "uname",
        f"exit={result.exit_code} stdout={result.stdout.strip()!r}",
    )
    result = await transport.exec("fail")
    failures += report(
        "non-zero status survives the sentinel",
        result.exit_code == 1 and "nope" in result.stdout,
        f"exit={result.exit_code} stdout={result.stdout.strip()!r}",
    )
    parked = asyncio.create_task(transport.exec("sleep 30"))
    await asyncio.sleep(0.5)
    await transport.cancel_in_flight()
    result = await parked
    failures += report(
        "cancel_in_flight halts a parked command (BREAK is a no-op on a pty; Ctrl-C did it)",
        result.exit_code == -1,
        f"exit={result.exit_code} stderr={result.stderr.strip()!r}",
    )
    failures += report("port is closed after the cancel", not transport.connected, "")
    with contextlib.suppress(Exception):
        await transport.disconnect()
    return failures


async def _with_console(dut: str, peer: str) -> int:
    """Run the console on the peer end of the pair, then the contract on the DUT end."""
    fd = os.open(peer, os.O_RDWR | os.O_NOCTTY)
    console = FakeConsole()
    console_task = asyncio.create_task(console.run_fd(fd), name="bench-serial-console")
    try:
        return await _exercise(dut)
    finally:
        console.stop()
        console_task.cancel()
        with contextlib.suppress(BaseException):
            await console_task
        os.close(fd)


def _wait_for_links(proc: subprocess.Popen[bytes], links: tuple[Path, ...]) -> None:
    deadline = time.monotonic() + 5.0
    while not all(link.exists() for link in links):
        if proc.poll() is not None or time.monotonic() > deadline:
            sys.exit("socat did not create the pty pair")
        time.sleep(0.05)


def main() -> int:
    """Spawn the socat pair, hand one end to the console and the other to the transport."""
    socat = require_tool("socat")
    with tempfile.TemporaryDirectory(prefix="litmus-bench-serial-") as tmp:
        dut_link = Path(tmp) / "dut"
        peer_link = Path(tmp) / "peer"
        socat_proc = subprocess.Popen(
            [
                socat,
                "-d",
                "-d",
                f"pty,raw,echo=0,link={dut_link}",
                f"pty,raw,echo=0,link={peer_link}",
            ],
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for_links(socat_proc, (dut_link, peer_link))
            dut = os.path.realpath(dut_link)
            peer = os.path.realpath(peer_link)
            print(f"socat pair: DUT side {dut}, console side {peer}")
            return asyncio.run(_with_console(dut, peer))
        finally:
            socat_proc.terminate()
            socat_proc.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
