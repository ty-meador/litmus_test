"""GPIO lines on a chip the framework has no backend for yet (``make bench-suite-gpio``).

``docs/hil/gpio.md`` is signed and ``utils.get_gpio`` is not built, so this is how a
developer reaches a header today: ``gpiod`` directly inside the test methods, the chip
located in ``configure()``. The claims are the ones a header acceptance would make on
real silicon: an input reads what is driven onto it, an output drives what was set,
and an edge arrives as an event with a timestamp the suite can measure latency from.
Here the far side is ``gpio-sim``'s sysfs (``make bench-root-setup``), so pull and
value are instant and the latency measured is the kernel's event path, not a wire.

Blocking ``gpiod`` calls run under ``asyncio.to_thread`` so a parked edge wait cannot
stall the audit clock or the sibling leaf. The framework's abort sweep does not know
about the requests; the ``with`` blocks release them on any exit.

Needs the ``bench`` dependency group: ``uv run --group bench litmus run ...``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import gpiod
from gpiod.line import Direction, Edge, Value

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict, evidence

type _Config = dict[Iterable[int | str] | int | str, gpiod.LineSettings | None]


class GpioHeader(LitmusTest):
    """Input, output, and edge events on three lines of one chip."""

    def __init__(
        self,
        *,
        name: str = "gpio",
        sim: str = "/sys/kernel/config/gpio-sim/litmus",
        input_line: int = 0,
        output_line: int = 1,
        edge_line: int = 2,
        consumer: str = "litmus-bench",
        edge_latency_limit_us: float = 5000.0,
        edge_wait_s: float = 1.0,
    ) -> None:
        """Capture which lines are which and the edge latency limit.

        Args:
            name: Node name used by logs, artifacts, and failures.
            sim: The gpio-sim configfs directory the chip was born in.
            input_line: Offset driven from the sim side and read as an input.
            output_line: Offset driven as an output and witnessed on the sim side.
            edge_line: Offset toggled from the sim side and watched for edge events.
            consumer: Label the kernel reports for the lines while requested.
            edge_latency_limit_us: Ceiling on stimulus-to-event latency.
            edge_wait_s: How long an edge may take to arrive before the claim fails.
        """
        super().__init__(name=name)
        self._sim = Path(sim)
        self._input_line = input_line
        self._output_line = output_line
        self._edge_line = edge_line
        self._consumer = consumer
        self._edge_latency_limit_us = edge_latency_limit_us
        self._edge_wait_s = edge_wait_s

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Locate the chip's device node and its sysfs line directory."""
        try:
            chip = (self._sim / "bank0" / "chip_name").read_text().strip()
            dev = (self._sim / "dev_name").read_text().strip()
        except OSError as error:
            raise TestFailure(f"no gpio-sim chip at {self._sim}: {error}") from error
        node = Path("/dev") / chip
        lines = Path("/sys/devices/platform") / dev / chip
        with gpiod.Chip(str(node)) as handle:
            info = handle.get_info()
            utils.logger.info("chip %s: %s, %d lines", info.name, info.label, info.num_lines)
            num_lines = info.num_lines
        return {
            "node": str(node),
            "lines": str(lines),
            "label": info.label,
            "num_lines": num_lines,
            "edge_latency_limit_us": self._edge_latency_limit_us,
            "edge_wait_s": self._edge_wait_s,
        }

    def build_suite(self) -> LitmusTest:
        """Three lines, three independent claims."""
        return self.parallel(
            self.name,
            [
                self.test("input-follows-drive", self._input_follows, fail_after=10.0),
                self.test("output-reaches-sim", self._output_reaches, fail_after=10.0),
                self.test("edge-events", self._edge_events, fail_after=15.0),
            ],
        )

    @evidence("line", "low", "high")
    async def _input_follows(self, utils: TestUtils) -> Verdict:
        """An input reads the level driven onto it from the far side."""
        node, lines = utils.ctx["node"], Path(utils.ctx["lines"])
        offset = self._input_line

        def read_both() -> tuple[Value, Value]:
            cfg: _Config = {offset: gpiod.LineSettings(direction=Direction.INPUT)}
            with gpiod.request_lines(node, consumer=self._consumer, config=cfg) as req:
                _set_pull(lines, offset, up=False)
                low = req.get_value(offset)
                _set_pull(lines, offset, up=True)
                high = req.get_value(offset)
            return low, high

        low, high = await asyncio.to_thread(read_both)
        if low is Value.INACTIVE and high is Value.ACTIVE:
            return PASS({"line": offset, "low": low.name, "high": high.name})
        raise TestFailure(
            f"line {offset} does not follow its drive",
            "INACTIVE then ACTIVE",
            f"{low.name} then {high.name}",
        )

    @evidence("line", "high", "low")
    async def _output_reaches(self, utils: TestUtils) -> Verdict:
        """What an output sets is what the far side witnesses."""
        node, lines = utils.ctx["node"], Path(utils.ctx["lines"])
        offset = self._output_line

        def drive_both() -> tuple[str, str]:
            cfg: _Config = {
                offset: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE)
            }
            with gpiod.request_lines(node, consumer=self._consumer, config=cfg) as req:
                req.set_value(offset, Value.ACTIVE)
                high = _sim_value(lines, offset)
                req.set_value(offset, Value.INACTIVE)
                low = _sim_value(lines, offset)
            return high, low

        high, low = await asyncio.to_thread(drive_both)
        if high == "1" and low == "0":
            return PASS({"line": offset, "high": high, "low": low})
        raise TestFailure(
            f"line {offset} output not witnessed",
            "1 then 0",
            f"{high} then {low}",
        )

    @evidence("line", "events", "edge_latency_us")
    async def _edge_events(self, utils: TestUtils) -> Verdict:
        """A rising then a falling edge arrive as events, inside the latency limit."""
        node, lines = utils.ctx["node"], Path(utils.ctx["lines"])
        offset = self._edge_line
        wait_s: float = utils.ctx["edge_wait_s"]

        def toggle_and_watch() -> tuple[list[str], float]:
            _set_pull(lines, offset, up=False)
            cfg: _Config = {
                offset: gpiod.LineSettings(direction=Direction.INPUT, edge_detection=Edge.BOTH)
            }
            seen: list[str] = []
            latencies_us: list[float] = []
            with gpiod.request_lines(node, consumer=self._consumer, config=cfg) as req:
                for up in (True, False):
                    stimulus_ns = time.monotonic_ns()
                    _set_pull(lines, offset, up=up)
                    if not req.wait_edge_events(wait_s):
                        break
                    for event in req.read_edge_events():
                        seen.append(event.event_type.name)
                        latencies_us.append((event.timestamp_ns - stimulus_ns) / 1000.0)
            return seen, max(latencies_us, default=-1.0)

        seen, worst_us = await asyncio.to_thread(toggle_and_watch)
        reading = utils.measure(
            "edge_latency_us",
            round(worst_us, 1),
            unit="us",
            limits=(0.0, utils.ctx["edge_latency_limit_us"]),
        )
        if seen == ["RISING_EDGE", "FALLING_EDGE"] and reading.in_limits:
            return PASS({"line": offset, "events": seen, "edge_latency_us": reading.value})
        raise TestFailure(
            f"line {offset} edge events wrong or late",
            f"RISING_EDGE, FALLING_EDGE within {reading.limits} us",
            f"{seen or 'nothing'} worst {reading.value} us",
        )


def _set_pull(lines: Path, offset: int, *, up: bool) -> None:
    """Drive a line from the sim side."""
    (lines / f"sim_gpio{offset}" / "pull").write_text("pull-up" if up else "pull-down")


def _sim_value(lines: Path, offset: int) -> str:
    """What the sim sees on a line, as sysfs prints it ("0" or "1")."""
    return (lines / f"sim_gpio{offset}" / "value").read_text().strip()
