"""``examples/pi_hole_server.py`` against the Pi-hole container (``make bench-pihole``)."""

from __future__ import annotations

from examples.pi_hole_server import PiHoleServer


class PiHoleLab(PiHoleServer):
    """The shipped Pi-hole example with host verification off; everything else is inherited."""

    def __init__(self, name: str = "pihole-lab") -> None:
        super().__init__(name=name, skip_host_verification=True)
