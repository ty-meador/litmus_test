"""Shared helpers for the bench surrogates."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path


def cache_dir() -> Path:
    """Where images, keys, overlays, and container build contexts live (not the tree)."""
    root = Path(os.environ.get("LITMUS_BENCH_CACHE", Path.home() / ".cache" / "litmus-bench"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def require_tool(name: str) -> str:
    """Resolve a host tool or exit naming it; the README lists the apt packages."""
    path = shutil.which(name)
    if path is None:
        sys.exit(f"{name} is not installed; see tools/bench/README.md")
    return path


def run(
    argv: Sequence[str], *, check: bool = True, capture: bool = False, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """`subprocess.run` with text output; the bench never shells out through a string."""
    return subprocess.run(
        list(argv), check=check, text=True, capture_output=capture, timeout=timeout
    )


def wait_for_port(host: str, port: int, *, timeout: float) -> bool:
    """Poll a TCP port until it accepts or the timeout passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def report(claim: str, ok: bool, detail: str) -> int:
    """Print one verdict line; returns 1 on a failure so callers can sum them."""
    print(f"[{'PASS' if ok else 'FAIL'}] {claim}: {detail}")
    return 0 if ok else 1


def pid_from(pidfile: Path) -> int | None:
    """The live pid recorded in `pidfile`, or None when absent or dead."""
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, 0)
    except OSError, ValueError:
        return None
    return pid
