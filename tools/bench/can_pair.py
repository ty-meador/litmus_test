"""CAN surrogate: a ``vxcan`` pair, two SocketCAN endpoints that model two nodes.

``vxcan0`` and ``vxcan1`` are the two ends of a virtual cable, so a frame sent on
one is received on the other and *only* the other — unlike a single ``vcan``, which
echoes to every opener. Raw CAN sockets need no capabilities once the links exist;
creating them does, which is ``make bench-root-setup``. Parked until a CAN backend
exists in ``src/``; today this proves the host path a SocketCAN backend would use.
"""

from __future__ import annotations

import socket
import struct
import sys

from tools.bench._common import report

_FRAME = struct.Struct("=IB3x8s")
_PAIR = ("vxcan0", "vxcan1")


def _open(interface: str) -> socket.socket:
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    sock.settimeout(2.0)
    try:
        sock.bind((interface,))
    except OSError as error:
        sys.exit(f"{interface}: {error}; run `make bench-root-setup` first")
    return sock


def _roundtrip(tx: socket.socket, rx: socket.socket, can_id: int, payload: bytes) -> str:
    tx.send(_FRAME.pack(can_id, len(payload), payload.ljust(8, b"\0")))
    raw = rx.recv(_FRAME.size)
    got_id, dlc, data = _FRAME.unpack(raw)
    if got_id != can_id or data[:dlc] != payload:
        raise AssertionError(f"sent id={can_id:#x} {payload!r}, got id={got_id:#x} {data[:dlc]!r}")
    return f"id={can_id:#x} dlc={dlc} data={data[:dlc].hex()}"


def main() -> int:
    """Send one frame each way across the pair and check it lands on the far end only."""
    failures = 0
    with _open(_PAIR[0]) as a, _open(_PAIR[1]) as b:
        for tx, rx, can_id, payload in (
            (a, b, 0x123, b"\x01\x02\x03"),
            (b, a, 0x7FF, b"litmus"),
        ):
            try:
                detail = _roundtrip(tx, rx, can_id, payload)
                ok = True
            except (AssertionError, TimeoutError) as error:
                detail, ok = str(error), False
            failures += report(f"{tx.getsockname()[0]} -> {rx.getsockname()[0]}", ok, detail)
        try:
            a.recv(_FRAME.size)
            failures += report("no echo on the sending end", False, "a frame came back")
        except TimeoutError:
            failures += report("no echo on the sending end", True, "nothing in 2 s")
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
