"""Survey and audit hooks around a device with real state transitions.

`configure` brings a power rail up and waits until it is on. A survey snapshots
the rail and enumeration at every lifecycle boundary. An audit watches
temperature while the claims run; a reading over the limit fails the suite and
cancels the in-flight claim. `cleanup` drops the rail.

The observations are scripted so the example runs on any development machine.
Replace the constructor values with `utils.exec()` reads in a real suite; the
hook registration, the ctx flow, and the evidence shape stay the same.
"""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict, cast

from litmus import PASS, LitmusTest, RunResult, TestFailure, TestUtils, Verdict


class _DeviceContext(TypedDict):
    """Serializable evidence accumulated across the powered-device lifecycle."""

    rail: str
    enumerated: bool
    temperature_c: float | None
    thermal_samples: int
    cleanup_complete: bool


class PoweredDevice(LitmusTest):
    """Bring a rail up, watch it, claim enumeration, drop the rail in cleanup."""

    def __init__(
        self,
        *,
        name: str,
        rail_states: tuple[str, ...],
        temperatures: tuple[float, ...],
        enumerated: bool,
        limit_c: float = 85.0,
        interval: float = 0.01,
        timeout_seconds: float = 1.0,
        audit_interval: float = 0.01,
        claim_seconds: float = 0.05,
    ) -> None:
        """Capture the scripted rail, thermal, and enumeration observations.

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

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Block until the rail is on and publish the starting evidence."""
        states = iter(self._rail_states)
        rail = "not-observed"

        async def rail_is_on() -> bool:
            nonlocal rail
            await asyncio.sleep(0)
            rail = next(states, rail)
            utils.logger.info("rail state: %s", rail)
            return rail == "on"

        await utils.wait_for(
            rail_is_on,
            looking_for="power rail on",
            interval=self._interval,
            timeout_seconds=self._timeout_seconds,
        )
        return {
            "rail": rail,
            "enumerated": False,
            "temperature_c": None,
            "thermal_samples": 0,
            "cleanup_complete": False,
        }

    def build_suite(self) -> LitmusTest:
        """Claim the device enumerated only after the rail is up."""
        return self.test("device-enumerates", self._device_enumerates)

    async def cleanup(self, utils: TestUtils) -> None:
        """Drop the rail. Always runs; must not throw."""
        ctx = cast("_DeviceContext", utils.ctx)
        ctx["rail"] = "off"
        ctx["cleanup_complete"] = True
        utils.logger.info("rail dropped")

    async def _snapshot(self, utils: TestUtils) -> dict[str, object]:
        """Capture rail, enumeration, and the latest thermal at each boundary."""
        return {
            "rail": str(utils.ctx.get("rail", "unknown")),
            "enumerated": bool(utils.ctx.get("enumerated", False)),
            "temperature_c": utils.ctx.get("temperature_c"),
            "cleanup_complete": bool(utils.ctx.get("cleanup_complete", False)),
        }

    async def _thermals(self, utils: TestUtils) -> dict[str, object]:
        """Sample temperature. A reading over the limit is a real Fail."""
        ctx = cast("_DeviceContext", utils.ctx)
        index = ctx["thermal_samples"]
        if self._temperatures:
            reading = self._temperatures[min(index, len(self._temperatures) - 1)]
        else:
            reading = 0.0
        ctx["thermal_samples"] = index + 1
        ctx["temperature_c"] = reading
        if reading > self._limit_c:
            raise TestFailure(
                "temperature over limit",
                f"<= {self._limit_c:g} degC",
                f"{reading:g} degC",
            )
        return {"temperature_c": reading, "limit_c": self._limit_c}

    async def _device_enumerates(self, utils: TestUtils) -> Verdict:
        await asyncio.sleep(self._claim_seconds)
        ctx = cast("_DeviceContext", utils.ctx)
        ctx["enumerated"] = self._enumerated
        if self._enumerated:
            utils.logger.info("device enumerated")
            return PASS
        raise TestFailure("device did not enumerate", True, False)


class HardwareDeviceLifecycleExample:
    """Run the powered-device lifecycle as a standalone program."""

    @staticmethod
    async def run() -> RunResult:
        """Execute through the framework-owned lifecycle."""
        return await PoweredDevice(
            name="powered device",
            rail_states=("off", "ramping", "on"),
            temperatures=(41.0, 42.5, 43.0),
            enumerated=True,
        ).run()


if __name__ == "__main__":
    asyncio.run(HardwareDeviceLifecycleExample.run())
