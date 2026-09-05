"""The host-side executor choke point: off the loop, never throws on a failed command,
SIGTERM on abort, no zombies.
"""

from __future__ import annotations

import asyncio
import gc
import os
import shlex
import sys
import threading
import time
import tracemalloc
from contextlib import suppress
from pathlib import Path

import pytest

# Internal machinery on purpose: the choke point is the subject under test, and the
# self-test suite is the one consumer allowed to reach past the public surface.
from litmus.executor import DEFAULT_EXEC_OUTPUT_BUDGET, BoundedCapture, ExecResult, LocalExec
from litmus.ssh import SshInterface


class TestExecResultSettled:
    """The single settlement path every transport shares."""

    def test_decodes_bytes_and_passes_status_through(self) -> None:
        result = ExecResult.settled(b"out", b"err", 3)
        assert result == ExecResult(stdout="out", stderr="err", exit_code=3)

    def test_str_output_passes_through_unchanged(self) -> None:
        result = ExecResult.settled("out", "err", 0)
        assert result == ExecResult(stdout="out", stderr="err", exit_code=0)

    def test_missing_output_settles_as_empty_text(self) -> None:
        result = ExecResult.settled(None, None, 0)
        assert result == ExecResult(stdout="", stderr="", exit_code=0)

    def test_absent_and_negative_status_settle_as_minus_one(self) -> None:
        assert ExecResult.settled("", "", None).exit_code == -1
        assert ExecResult.settled("", "", -9).exit_code == -1

    def test_invalid_utf8_degrades_with_replacement(self) -> None:
        assert ExecResult.settled(b"\xff", b"", 0).stdout == "�"

    def test_settled_records_truncation_without_breaking_existing_fields(self) -> None:
        result = ExecResult.settled(b"abc", b"", 0, truncated=True, captured_bytes=3)
        assert result.truncated is True
        assert result.captured_bytes == 3
        assert result.stdout == "abc"
        assert result.exit_code == 0

    def test_existing_constructor_defaults_are_not_truncated(self) -> None:
        result = ExecResult(stdout="ok", stderr="", exit_code=0)
        assert result.truncated is False
        assert result.captured_bytes == 2


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
        assert litmus.ExecResult is ExecResult
        assert "ExecResult" in litmus.__all__


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


def _dump_command(nbytes: int, *, stream: str = "stdout") -> str:
    if stream == "stderr":
        script = f"import sys; sys.stderr.buffer.write(b'y'*{nbytes})"
    else:
        script = f"import sys; sys.stdout.buffer.write(b'x'*{nbytes})"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def _write_then_sleep_command(marker_path: Path, message: str, seconds: int = 60) -> str:
    script = (
        "import sys, time; "
        f"sys.stdout.buffer.write({message.encode()!r}); sys.stdout.buffer.flush(); "
        f"open({str(marker_path)!r}, 'w').write('ready'); "
        f"time.sleep({seconds})"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


def _flood_command(pid_path: Path) -> str:
    script = (
        "import os, sys\n"
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "while True:\n"
        "    sys.stdout.buffer.write(b'x'*4096)\n"
        "    sys.stdout.buffer.flush()\n"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"


class TestBoundedCapture:
    """The shared stdout+stderr accumulator both transports drain through."""

    def test_an_empty_chunk_does_not_count_or_truncate(self) -> None:
        capture = BoundedCapture(8)
        assert capture.accept(b"", into=capture.stdout) is True
        assert capture.captured_bytes == 0
        assert capture.truncated is False

    def test_a_full_capture_rejects_further_chunks(self) -> None:
        capture = BoundedCapture(4)
        assert capture.accept(b"abcd", into=capture.stdout) is True
        assert capture.accept(b"x", into=capture.stdout) is False
        assert capture.truncated is True
        assert capture.captured_bytes == 4
        assert bytes(capture.stdout) == b"abcd"

    def test_a_zero_room_chunk_marks_truncated(self) -> None:
        capture = BoundedCapture(2)
        capture.captured_bytes = 2
        assert capture.accept(b"z", into=capture.stdout) is False
        assert capture.truncated is True
        assert bytes(capture.stdout) == b""

    async def test_a_missing_stream_is_a_completed_pump(self) -> None:
        capture = BoundedCapture(8)
        await capture.drain(None, None)
        assert capture.captured_bytes == 0
        assert capture.truncated is False

    async def test_a_str_chunk_is_counted_as_utf8_bytes(self) -> None:
        class _StrReader:
            def __init__(self) -> None:
                self._chunks = ["hi", ""]

            async def read(self, n: int = -1) -> str:
                del n
                return self._chunks.pop(0)

        capture = BoundedCapture(8)
        await capture.drain(_StrReader(), None)
        assert capture.stdout == bytearray(b"hi")
        assert capture.captured_bytes == 2

    async def test_a_treat_as_eof_error_ends_the_pump(self) -> None:
        class _Boom:
            async def read(self, n: int = -1) -> bytes:
                del n
                raise ConnectionError("closed")

        capture = BoundedCapture(8)
        await capture.drain(_Boom(), None, treat_as_eof=(ConnectionError,))
        assert capture.truncated is False
        assert capture.captured_bytes == 0

    async def test_truncation_without_a_stop_callback_still_settles(self) -> None:
        class _Flood:
            def __init__(self) -> None:
                self._sent = False

            async def read(self, n: int = -1) -> bytes:
                del n
                if self._sent:
                    return b""
                self._sent = True
                return b"x" * 32

        capture = BoundedCapture(8)
        await capture.drain(_Flood(), None)
        assert capture.truncated is True
        assert capture.captured_bytes == 8


class TestOutputBudget:
    """One named ceiling, defaulted, per LocalExec instance — not a process-global."""

    def test_local_and_ssh_share_the_same_default_budget(self) -> None:
        local = LocalExec()
        ssh = SshInterface(host="127.0.0.1", username="root", password="x")
        assert local.output_budget == DEFAULT_EXEC_OUTPUT_BUDGET
        assert ssh.output_budget == local.output_budget

    def test_budget_is_per_instance_not_process_global(self) -> None:
        left = LocalExec(output_budget=8)
        right = LocalExec(output_budget=64)
        assert left.output_budget == 8
        assert right.output_budget == 64
        assert LocalExec().output_budget == DEFAULT_EXEC_OUTPUT_BUDGET

    def test_a_budget_below_one_byte_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 1 byte"):
            LocalExec(output_budget=0)
        with pytest.raises(ValueError, match="at least 1 byte"):
            SshInterface(host="127.0.0.1", username="root", password="x", output_budget=-1)

    async def test_output_under_the_budget_is_kept_in_full(self) -> None:
        result = await LocalExec(output_budget=64).run(_dump_command(16))
        assert result.truncated is False
        assert result.captured_bytes == 16
        assert result.stdout == "x" * 16
        assert result.exit_code == 0

    async def test_exceeding_the_budget_truncates_and_does_not_throw(self) -> None:
        result = await LocalExec(output_budget=64).run(_dump_command(10_000))
        assert result.truncated is True
        assert result.captured_bytes == 64
        assert len(result.stdout.encode("utf-8")) == 64
        assert result.stdout == "x" * 64

    async def test_stdout_and_stderr_share_one_budget(self) -> None:
        script = "import sys; sys.stdout.buffer.write(b'o'*80); sys.stderr.buffer.write(b'e'*80)"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
        result = await LocalExec(output_budget=100).run(command)
        assert result.truncated is True
        assert result.captured_bytes == 100
        assert len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")) == 100

    async def test_a_cut_utf8_sequence_decodes_with_replacement(self) -> None:
        # U+2603 SNOWMAN is e2 98 83. A 2-byte budget keeps an incomplete sequence.
        script = "import sys; sys.stdout.buffer.write(b'\\xe2\\x98\\x83'*4)"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
        result = await LocalExec(output_budget=2).run(command)
        assert result.truncated is True
        assert result.captured_bytes == 2
        assert "\ufffd" in result.stdout or "�" in result.stdout

    async def test_a_runaway_writer_is_killed_like_abort(self, tmp_path: Path) -> None:
        host = LocalExec(output_budget=1024)
        pid_path = tmp_path / "pid"
        result = await asyncio.wait_for(host.run(_flood_command(pid_path)), timeout=5)
        assert result.truncated is True
        assert result.captured_bytes == 1024
        assert result.exit_code == -1
        pid = await _Child.wait_for_pidfile(pid_path)
        await _Child.wait_until_gone(pid)


class TestStreamingPreservesOutput:
    async def test_abort_keeps_bytes_already_received(self, tmp_path: Path) -> None:
        host = LocalExec()
        marker = tmp_path / "ready"
        task = asyncio.create_task(host.run(_write_then_sleep_command(marker, "hello-partial")))
        deadline = time.monotonic() + 2
        while not marker.exists():
            assert time.monotonic() < deadline, "child never flushed its prefix"
            await asyncio.sleep(0.02)
        await host.cancel_in_flight()
        result = await task
        assert result.exit_code == -1
        assert result.truncated is False
        assert result.stdout == "hello-partial"


class TestOutputBudgetRackScale:
    """Seventy overflowing execs must not balloon retained memory."""

    async def test_seventy_overflowing_execs_complete_with_flat_retained_memory(self) -> None:
        budget = 512
        dump = 64 * 1024
        host = LocalExec(output_budget=budget)
        command = f"head -c {dump} /dev/zero"

        gc.collect()
        tracemalloc.start()
        try:
            results = await asyncio.gather(*[host.run(command) for _ in range(70)])
            gc.collect()
            snapshot = tracemalloc.take_snapshot().filter_traces(
                (tracemalloc.Filter(True, "*/src/litmus/*"),)
            )
            retained = sum(stat.size for stat in snapshot.statistics("filename"))
        finally:
            tracemalloc.stop()

        assert len(results) == 70
        assert all(result.truncated for result in results)
        assert all(result.captured_bytes == budget for result in results)
        held = sum(
            len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8"))
            for result in results
        )
        assert held <= 70 * budget
        # 70 * 64 KiB dumped would be 4.5 MiB in the result objects without a budget.
        assert retained < dump, (
            f"litmus retained {retained} bytes after 70 overflows; "
            f"a single untruncated dump is {dump} bytes"
        )
