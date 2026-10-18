"""The rack: every bench device under one parallel owner (``make bench-rack``).

This is the shape the bench exists to model: a developer's rack of unrelated devices,
each with its own transport, run as one suite so one record answers "is the rack
healthy". Each child declares its own route in its own ``configure()``; the rack
itself has no transport and no claims. Because the children are independent, a dead
Pi-hole does not hide a slow CoAP device: ``parallel`` records every verdict.

The children are the real suites in this directory, composed as they are. The rack
adds nothing but the owner. ``make bench-soak`` runs it many times over, which is
where a transport that leaks a task or a session shows up.
"""

from __future__ import annotations

from bench.suites.coap_device import CoapDevice
from bench.suites.guest_acceptance import DebianGuest
from bench.suites.pihole_rack import PiHole
from bench.suites.serial_console import SerialConsole
from litmus import LitmusTest


class Rack(LitmusTest):
    """Four devices, four transports, one verdict."""

    def __init__(self, *, name: str = "rack") -> None:
        """Compose the rack's devices. Each resolves its own target from the run environment."""
        super().__init__(name=name)
        self._guest = DebianGuest(name="guest")
        self._pihole = PiHole(name="pihole")
        self._coap = CoapDevice(name="coap")
        self._serial = SerialConsole(name="serial")

    def build_suite(self) -> LitmusTest:
        """Independent devices, so independent verdicts."""
        return self.parallel(self.name, [self._guest, self._pihole, self._coap, self._serial])
