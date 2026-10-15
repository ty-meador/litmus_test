"""GPIO surrogate: the kernel's ``gpio-sim``, a fake chip whose lines you drive from sysfs.

``make bench-root-setup`` builds a chip labelled "litmus-bench" with eight lines under
``/sys/kernel/config/gpio-sim/litmus`` and opens its ``/dev/gpiochipN`` and per-line
``pull`` knobs to the bench user. From userspace it is an ordinary character device:
the same ``GPIO_V2_*`` ioctls a real chip answers, reached here through libgpiod's
``gpiod`` bindings (the ``bench`` dependency group). The far side is sysfs:
``sim_gpioY/pull`` drives what an input reads, ``sim_gpioY/value`` witnesses what an
output set. Three claims: a pull reaches an input, an output reaches the sim, and
flipping the pull raises rising then falling edge events.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

import gpiod
from gpiod.line import Direction, Edge, Value

from bench._common import report

_SIM = Path("/sys/kernel/config/gpio-sim/litmus")
_CONSUMER = "litmus-bench"
_INPUT, _OUTPUT, _EDGE = 0, 1, 2
type _Config = dict[Iterable[int | str] | int | str, gpiod.LineSettings | None]


def sim_chip() -> tuple[Path, Path]:
    """The chip's device node and its sysfs line directory, or exit naming the setup step."""
    try:
        chip = (_SIM / "bank0" / "chip_name").read_text().strip()
        dev = (_SIM / "dev_name").read_text().strip()
    except OSError:
        sys.exit("no gpio-sim chip; run `make bench-root-setup` first")
    return Path("/dev") / chip, Path("/sys/devices/platform") / dev / chip


def set_pull(lines: Path, offset: int, up: bool) -> None:
    """Drive a line from the sim side."""
    (lines / f"sim_gpio{offset}" / "pull").write_text("pull-up" if up else "pull-down")


def sim_value(lines: Path, offset: int) -> str:
    """What the sim sees on a line, as sysfs prints it ("0" or "1")."""
    return (lines / f"sim_gpio{offset}" / "value").read_text().strip()


def check_input(node: Path, lines: Path) -> int:
    """A pull on the sim side is what an input line reads."""
    cfg: _Config = {_INPUT: gpiod.LineSettings(direction=Direction.INPUT)}
    with gpiod.request_lines(str(node), consumer=_CONSUMER, config=cfg) as req:
        set_pull(lines, _INPUT, up=False)
        low = req.get_value(_INPUT)
        set_pull(lines, _INPUT, up=True)
        high = req.get_value(_INPUT)
    return report(
        f"line {_INPUT} input follows the sim pull",
        low is Value.INACTIVE and high is Value.ACTIVE,
        f"pull-down read {low.name}, pull-up read {high.name}",
    )


def check_output(node: Path, lines: Path) -> int:
    """What an output line sets is what the sim witnesses."""
    cfg: _Config = {
        _OUTPUT: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE)
    }
    with gpiod.request_lines(str(node), consumer=_CONSUMER, config=cfg) as req:
        req.set_value(_OUTPUT, Value.ACTIVE)
        high = sim_value(lines, _OUTPUT)
        req.set_value(_OUTPUT, Value.INACTIVE)
        low = sim_value(lines, _OUTPUT)
    return report(
        f"line {_OUTPUT} output reaches the sim",
        high == "1" and low == "0",
        f"set active saw {high}, set inactive saw {low}",
    )


def check_edges(node: Path, lines: Path) -> int:
    """Flipping the pull up then down delivers a rising then a falling edge event."""
    set_pull(lines, _EDGE, up=False)
    cfg: _Config = {_EDGE: gpiod.LineSettings(direction=Direction.INPUT, edge_detection=Edge.BOTH)}
    seen: list[str] = []
    with gpiod.request_lines(str(node), consumer=_CONSUMER, config=cfg) as req:
        set_pull(lines, _EDGE, up=True)
        set_pull(lines, _EDGE, up=False)
        while len(seen) < 2 and req.wait_edge_events(1.0):
            seen.extend(e.event_type.name for e in req.read_edge_events())
    return report(
        f"line {_EDGE} delivers edge events",
        seen == ["RISING_EDGE", "FALLING_EDGE"],
        f"got {seen or 'nothing in 1 s'}",
    )


def main() -> int:
    """Find the sim chip, then run the three claims."""
    node, lines = sim_chip()
    with gpiod.Chip(str(node)) as chip:
        info = chip.get_info()
    print(f"gpio-sim chip: {node} label={info.label!r} lines={info.num_lines}")
    return check_input(node, lines) + check_output(node, lines) + check_edges(node, lines)


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
