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
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from tests.fake_console import FakeConsole

from bench._common import cache_dir, pid_from, report, require_tool
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


def _spawn_socat(
    socat: str, dut_link: Path, peer_link: Path, **popen: Any
) -> subprocess.Popen[Any]:
    """Start the null-modem pair with stable symlinks at `dut_link` and `peer_link`."""
    return subprocess.Popen(
        [
            socat,
            "-d",
            "-d",
            f"pty,raw,echo=0,link={dut_link}",
            f"pty,raw,echo=0,link={peer_link}",
        ],
        stderr=subprocess.DEVNULL,
        **popen,
    )


def check() -> int:
    """Spawn the socat pair, hand one end to the console and the other to the transport."""
    socat = require_tool("socat")
    with tempfile.TemporaryDirectory(prefix="litmus-bench-serial-") as tmp:
        dut_link = Path(tmp) / "dut"
        peer_link = Path(tmp) / "peer"
        socat_proc = _spawn_socat(socat, dut_link, peer_link)
        try:
            _wait_for_links(socat_proc, (dut_link, peer_link))
            dut = os.path.realpath(dut_link)
            peer = os.path.realpath(peer_link)
            print(f"socat pair: DUT side {dut}, console side {peer}")
            return asyncio.run(_with_console(dut, peer))
        finally:
            socat_proc.terminate()
            socat_proc.wait(timeout=5)


# ``up`` leaves the pair running so a suite (``bench/suites/serial_console.py``) can open
# the DUT side by path from another process, the way it would open a USB adapter. The
# console runs as its own detached process; both pids and the DUT path live in the cache.
_STATE = cache_dir() / "serial"
_DEVICE_FILE = _STATE / "device"
_SOCAT_PID = _STATE / "socat.pid"
_CONSOLE_PID = _STATE / "console.pid"


def up() -> int:
    """Start socat and the console detached; print the DUT tty path. Idempotent."""
    if pid_from(_SOCAT_PID) and pid_from(_CONSOLE_PID) and _DEVICE_FILE.exists():
        print(f"serial pair already up; SERIAL_DEVICE={_DEVICE_FILE.read_text().strip()}")
        return 0
    down()
    socat = require_tool("socat")
    _STATE.mkdir(parents=True, exist_ok=True)
    dut_link, peer_link = _STATE / "dut", _STATE / "peer"
    socat_proc = _spawn_socat(socat, dut_link, peer_link, start_new_session=True)
    _wait_for_links(socat_proc, (dut_link, peer_link))
    _SOCAT_PID.write_text(str(socat_proc.pid))
    peer = os.path.realpath(peer_link)
    log = (_STATE / "console.log").open("ab")
    console_proc = subprocess.Popen(
        [sys.executable, "-m", "bench.serial_pair", "console", peer],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
        env={**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", ".")},
    )
    _CONSOLE_PID.write_text(str(console_proc.pid))
    device = os.path.realpath(dut_link)
    _DEVICE_FILE.write_text(device)
    print(f"serial pair up: console on {peer}; SERIAL_DEVICE={device}")
    return 0


def down() -> int:
    """Stop the console and socat if they are running; forget the pair."""
    for pidfile in (_CONSOLE_PID, _SOCAT_PID):
        pid = pid_from(pidfile)
        if pid is not None:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
        pidfile.unlink(missing_ok=True)
    _DEVICE_FILE.unlink(missing_ok=True)
    print("serial pair down")
    return 0


def status() -> int:
    """Running or not, and the DUT path a suite should open."""
    live = pid_from(_SOCAT_PID) is not None and pid_from(_CONSOLE_PID) is not None
    device = _DEVICE_FILE.read_text().strip() if _DEVICE_FILE.exists() else "-"
    print(f"serial pair {'up' if live else 'down'}; SERIAL_DEVICE={device}")
    return 0 if live else 1


def console(peer: str) -> int:
    """Serve the self-test console on `peer` until SIGTERM (the detached half of ``up``)."""

    async def serve() -> None:
        fd = os.open(peer, os.O_RDWR | os.O_NOCTTY)
        fake = FakeConsole()
        stop = asyncio.Event()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
        task = asyncio.create_task(fake.run_fd(fd), name="bench-serial-console")
        try:
            await stop.wait()
        finally:
            fake.stop()
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            os.close(fd)

    asyncio.run(serve())
    return 0


def main(argv: list[str]) -> int:
    """Dispatch ``check`` (the default) / ``up`` / ``down`` / ``status`` / ``console PEER``."""
    if argv[:1] == ["console"] and len(argv) == 2:
        return console(argv[1])
    actions = {"check": check, "up": up, "down": down, "status": status}
    action = argv[0] if argv else "check"
    if len(argv) > 1 or action not in actions:
        sys.exit(f"usage: python -m bench.serial_pair [{'|'.join(actions)}|console PEER]")
    return actions[action]()


if __name__ == "__main__":
    sys.exit(1 if main(sys.argv[1:]) else 0)
