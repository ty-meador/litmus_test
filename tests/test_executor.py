"""The host-side executor choke point: off the loop, never throws on a failed command,
SIGTERM on abort, no zombies.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest

# Internal machinery on purpose: the choke point is the subject under test, and the
# self-test suite is the one consumer allowed to reach past the public surface.
from litmus.executor import ExecResult, LocalExec


class _Child:
    """Probe helpers for in-flight host processes. Not production API."""

    @staticmethod
    def pid_sleep_command(pid_path: Path, seconds: int = 60) -> str:
        script = (
            "import os, time; "
            f"open({str(pid_path)!r}, 'w').write(str(os.getpid())); "
            f"time.sleep({seconds})"
        )
        return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    @staticmethod
    def is_alive(pid: int) -> bool:
        """True if `pid` is a running process. Zombies are not running."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return not _Child.is_zombie(pid)

    @staticmethod
    def is_zombie(pid: int) -> bool:
        stat = Path(f"/proc/{pid}/stat")
        try:
            body = stat.read_text()
        except FileNotFoundError, ProcessLookupError:
            return False
        # /proc/<pid>/stat: comm can contain spaces/parentheses; state is after the last ')'
        state = body.rsplit(")", 1)[-1].split()[0]
        return state == "Z"

    @staticmethod
    def read_pid(pid_path: Path) -> int | None:
        if not pid_path.exists():
            return None
        text = pid_path.read_text().strip()
        if text:
            return int(text)
        return None

    @staticmethod
    async def wait_for_pidfile(pid_path: Path, seconds: float = 2.0) -> int:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pid = _Child.read_pid(pid_path)
            if pid is not None:
                return pid
            await asyncio.sleep(0.02)
        raise AssertionError("child never wrote its pid")

    @staticmethod
    async def wait_until_gone(pid: int, seconds: float = 2.0) -> None:
        """Init reaps grandchildren the shell did not; wait until that happens."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not _Child.is_alive(pid) and not _Child.is_zombie(pid):
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"pid {pid} did not leave the process table")


class TestExecResult:
    def test_is_frozen_evidence(self) -> None:
        result = ExecResult(stdout="ok\n", stderr="", exit_code=0)
        assert result.stdout == "ok\n"
        assert result.stderr == ""
        assert result.exit_code == 0
        with pytest.raises(AttributeError):
            result.exit_code = 1  # type: ignore[misc]


class TestPublicSurface:
    def test_local_exec_is_not_on_the_public_surface(self) -> None:
        import litmus

        assert not hasattr(litmus, "LocalExec")
        assert "LocalExec" not in litmus.__all__


class TestLocalExecNeverThrows:
    async def test_a_successful_command_returns_stdout(self) -> None:
        result = await LocalExec().run("echo hello")
        assert result.exit_code == 0
        assert result.stdout.strip() == "hello"
        assert result.stderr == ""

    async def test_a_nonzero_exit_is_evidence_not_an_exception(self) -> None:
        result = await LocalExec().run("exit 7")
        assert result.exit_code == 7

    async def test_stderr_is_captured(self) -> None:
        result = await LocalExec().run("echo err >&2")
        assert result.exit_code == 0
        assert result.stderr.strip() == "err"

    async def test_a_missing_command_is_still_a_result(self) -> None:
        result = await LocalExec().run("litmus-no-such-command-on-purpose")
        assert result.exit_code != 0
        assert result.exit_code != -1

    async def test_a_signal_killed_child_yields_minus_one(self) -> None:
        # `kill -TERM $$` delivers SIGTERM to the shell — the direct child we spawned —
        # so the executor sees a signal death rather than a numeric exit status.
        result = await LocalExec().run("kill -TERM $$")
        assert result.exit_code == -1


class TestSigtermAndReap:
    async def test_cancel_in_flight_sigterms_and_returns_minus_one(self, tmp_path: Path) -> None:
        host = LocalExec()
        pid_path = tmp_path / "pid"
        task = asyncio.create_task(host.run(_Child.pid_sleep_command(pid_path)))
        pid = await _Child.wait_for_pidfile(pid_path)
        await host.cancel_in_flight()
        result = await task
        assert result.exit_code == -1
        await _Child.wait_until_gone(pid)

    async def test_task_cancellation_reaps_the_child(self, tmp_path: Path) -> None:
        host = LocalExec()
        pid_path = tmp_path / "pid"
        task = asyncio.create_task(host.run(_Child.pid_sleep_command(pid_path)))
        pid = await _Child.wait_for_pidfile(pid_path)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _Child.wait_until_gone(pid)

    async def test_cancel_in_flight_is_safe_when_nothing_is_running(self) -> None:
        await LocalExec().cancel_in_flight()

    async def test_instances_do_not_share_in_flight_children(self, tmp_path: Path) -> None:
        # Process-per-DUT stays possible because nothing here is process-global:
        # cancelling one instance must not disturb another's child.
        left = LocalExec()
        right = LocalExec()
        left_pid_path = tmp_path / "left"
        right_pid_path = tmp_path / "right"
        left_task = asyncio.create_task(left.run(_Child.pid_sleep_command(left_pid_path)))
        right_task = asyncio.create_task(right.run(_Child.pid_sleep_command(right_pid_path)))
        left_pid = await _Child.wait_for_pidfile(left_pid_path)
        right_pid = await _Child.wait_for_pidfile(right_pid_path)
        await left.cancel_in_flight()
        left_result = await left_task
        assert left_result.exit_code == -1
        assert _Child.is_alive(right_pid)
        await _Child.wait_until_gone(left_pid)
        await right.cancel_in_flight()
        assert (await right_task).exit_code == -1
        await _Child.wait_until_gone(right_pid)


class TestRunBlocking:
    async def test_a_blocking_call_does_not_stall_the_loop(self) -> None:
        host = LocalExec()
        entered = threading.Event()
        gate = threading.Event()

        def block() -> str:
            entered.set()
            # If this ran on the event loop, the poll below would never get a tick.
            gate.wait(timeout=5)
            return "ok"

        task = asyncio.create_task(host.run_blocking(block))
        deadline = time.monotonic() + 2
        while not entered.is_set():
            assert time.monotonic() < deadline, "blocking callable never started"
            await asyncio.sleep(0.01)

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while not task.done():
                ticks += 1
                await asyncio.sleep(0)

        tick_task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        assert ticks > 0
        gate.set()
        assert await task == "ok"
        tick_task.cancel()
        with suppress(asyncio.CancelledError):
            await tick_task
