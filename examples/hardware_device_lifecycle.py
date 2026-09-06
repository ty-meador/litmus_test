"""Survey and audit hooks around a device with real state transitions.

`configure` brings a power rail up, waits until it is on, and returns the rail
handle and a thermal feed into ctx. A survey snapshots the rail at every
lifecycle boundary by asking the handle, so the post-cleanup snapshot shows the
rail down. An audit reads the thermal feed while the claim runs; a reading over
the limit fails the suite and cancels the in-flight claim. The claim carries its
enumeration reading on the verdict. `cleanup` drops the rail through the handle.

Ctx is read-only after `configure` returns: the things that change during the
run are objects that manage themselves (the rail, the thermal feed), and the
readings the Floor Technician needs travel with the verdict or the artifacts.

The observations are scripted so the example runs on any development machine.
Replace the constructor values with `utils.exec()` reads in a real suite; the
hook registration, the ctx flow, and the evidence shape stay the same.

Run it: `litmus run examples/hardware_device_lifecycle.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import TypedDict, cast

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict, evidence


class _ScriptedRail:
    """A rail whose state the suite reads and drops; stands in for a real controller."""

    def __init__(self, states: tuple[str, ...]) -> None:
        self._states = iter(states)
        self.state = "not-observed"

    def poll(self) -> str:
        """Advance the scripted ramp and return the current state."""
        self.state = next(self._states, self.state)
        return self.state

    def drop(self) -> None:
        """Turn the rail off."""
        self.state = "off"


def _repeat_last(readings: tuple[float, ...]) -> Iterator[float]:
    """Yield each scripted reading, then the last one forever; empty means 0.0."""
    last = 0.0
    for reading in readings:
        last = reading
        yield reading
    while True:
        yield last


class _DeviceContext(TypedDict):
    """What `configure` provides: the handles the hooks and the claim work through."""

    rail: _ScriptedRail
    thermals: Iterator[float]
    limit_c: float


class PoweredDevice(LitmusTest):
    """Bring a rail up, watch it, claim enumeration, drop the rail in cleanup."""

    def __init__(
        self,
        *,
        name: str,
        rail_states: tuple[str, ...] = ("off", "ramping", "on"),
        temperatures: tuple[float, ...] = (41.0, 42.5, 43.0),
        enumerated: bool = True,
        limit_c: float = 85.0,
        interval: float = 0.01,
        timeout_seconds: float = 1.0,
        audit_interval: float = 0.01,
        claim_seconds: float = 0.05,
    ) -> None:
        """Capture the scripted rail, thermal, and enumeration observations.

        The defaults are a healthy device, so `litmus run` needs no arguments.

        Args:
            name: Node name used by logs, artifacts, and failures.
            rail_states: Values `configure` polls through until `"on"`.
            temperatures: Readings the audit consumes in order, then repeats
                the last value.
            enumerated: Whether the enumeration claim passes.
            limit_c: Audit fails the suite above this temperature.
            interval: `wait_for` poll interval while the rail comes up.
            timeout_seconds: `wait_for` window for the rail.
            audit_interval: Seconds between thermal samples during the claim.
            claim_seconds: How long the enumeration claim holds so the audit
                can fire.
        """
        super().__init__(name=name)
        self._rail_states = rail_states
        self._temperatures = temperatures
        self._enumerated = enumerated
        self._limit_c = limit_c
        self._interval = interval
        self._timeout_seconds = timeout_seconds
        self._claim_seconds = claim_seconds
        self.use_survey(self._snapshot)
        self.use_audit(interval=audit_interval, fn=self._thermals)

    async def configure(self, utils: TestUtils) -> _DeviceContext:
        """Block until the rail is on and provide the handles the run works through."""
        rail = _ScriptedRail(self._rail_states)

        async def rail_is_on() -> bool:
            await asyncio.sleep(0)
            utils.logger.info("rail state: %s", rail.poll())
            return rail.state == "on"

        await utils.wait_for(
            rail_is_on,
            looking_for="power rail on",
            interval=self._interval,
            timeout_seconds=self._timeout_seconds,
        )
        return {
            "rail": rail,
            "thermals": _repeat_last(self._temperatures),
            "limit_c": self._limit_c,
        }

    def build_suite(self) -> LitmusTest:
        """Claim the device enumerated only after the rail is up."""
        return self.test("device-enumerates", self._device_enumerates)

    async def cleanup(self, utils: TestUtils) -> None:
        """Drop the rail. Always runs; must not throw."""
        rail = utils.ctx.get("rail")
        if isinstance(rail, _ScriptedRail):
            rail.drop()
            utils.logger.info("rail dropped")

    async def _snapshot(self, utils: TestUtils) -> dict[str, object]:
        """Capture the rail state at each boundary, from the handle itself."""
        rail = utils.ctx.get("rail")
        return {"rail": rail.state if isinstance(rail, _ScriptedRail) else "unknown"}

    async def _thermals(self, utils: TestUtils) -> dict[str, object]:
        """Sample temperature. A reading over the limit is a real Fail."""
        ctx = cast("_DeviceContext", utils.ctx)
        reading = next(ctx["thermals"])
        limit = ctx["limit_c"]
        if reading > limit:
            raise TestFailure("temperature over limit", f"<= {limit:g} degC", f"{reading:g} degC")
        return {"temperature_c": reading, "limit_c": limit}

    @evidence("enumerated")
    async def _device_enumerates(self, utils: TestUtils) -> Verdict:
        await asyncio.sleep(self._claim_seconds)
        if self._enumerated:
            utils.logger.info("device enumerated")
            return PASS({"enumerated": True})
        raise TestFailure("device did not enumerate", True, False, evidence={"enumerated": False})
