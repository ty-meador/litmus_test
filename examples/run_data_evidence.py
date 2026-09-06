"""The evidence surface as the subject: ctx flows into `run_data` automatically.

`configure` returns measurements; they merge into `utils.ctx` and appear on
`RunResult.run_data` with no extra work. Class instances, functions, and live
handles are stripped — only JSON-safe values survive. Override `run_data` only
when a computed field (here `within_spec`) is needed on top of that snapshot.

The readings are constructor-injected so the example runs on any development
machine. A real suite would fill ctx from `utils.exec()`; the snapshot and the
override stay the same.
"""

from __future__ import annotations

import asyncio
from typing import Any

from litmus import PASS, JsonObject, LitmusTest, RunResult, TestFailure, TestUtils, Verdict


class PowerSupply(LitmusTest):
    """Record voltage and temperature, then publish a computed within-spec flag."""

    def __init__(
        self,
        *,
        name: str,
        output_voltage_v: float,
        temperature_c: float,
        voltage_low_v: float = 11.5,
        voltage_high_v: float = 12.5,
        temp_limit_c: float = 70.0,
    ) -> None:
        """Capture the readings this suite will publish as evidence.

        Args:
            name: Node name used by logs, artifacts, and failures.
            output_voltage_v: Observed output voltage.
            temperature_c: Observed temperature.
            voltage_low_v: Inclusive lower spec for voltage.
            voltage_high_v: Inclusive upper spec for voltage.
            temp_limit_c: Inclusive upper spec for temperature.
        """
        super().__init__(name=name)
        self._output_voltage_v = output_voltage_v
        self._temperature_c = temperature_c
        self._voltage_low_v = voltage_low_v
        self._voltage_high_v = voltage_high_v
        self._temp_limit_c = temp_limit_c

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Seed ctx. The claims fill the real readings; both appear in run_data."""
        return {"output_voltage_v": 0.0, "temperature_c": 0.0}

    def build_suite(self) -> LitmusTest:
        """Independent claims: voltage and temperature do not depend on each other."""
        return self.parallel(
            self.name,
            [
                self.test("output-voltage", self._check_voltage),
                self.test("temperature", self._check_temperature),
            ],
        )

    async def _check_voltage(self, utils: TestUtils) -> Verdict:
        voltage = self._output_voltage_v
        utils.ctx["output_voltage_v"] = voltage
        utils.logger.info("PSU voltage: %s V", voltage)
        if self._voltage_low_v <= voltage <= self._voltage_high_v:
            return PASS
        raise TestFailure(
            "PSU voltage out of spec",
            f"{self._voltage_low_v:g}-{self._voltage_high_v:g} V",
            f"{voltage:g} V",
        )

    async def _check_temperature(self, utils: TestUtils) -> Verdict:
        temperature = self._temperature_c
        utils.ctx["temperature_c"] = temperature
        utils.logger.info("PSU temperature: %s degC (limit: %s)", temperature, self._temp_limit_c)
        if temperature <= self._temp_limit_c:
            return PASS
        raise TestFailure(
            "PSU temperature over limit",
            f"<= {self._temp_limit_c:g} degC",
            f"{temperature:g} degC",
        )

    @property
    def run_data(self) -> JsonObject:
        """Default snapshot plus a computed within-spec flag for the FT."""
        base = super().run_data
        voltage = base.get("output_voltage_v")
        temperature = base.get("temperature_c")
        within_spec = False
        if isinstance(voltage, int | float) and isinstance(temperature, int | float):
            within_spec = (
                self._voltage_low_v <= voltage <= self._voltage_high_v
                and temperature <= self._temp_limit_c
            )
        return {**base, "within_spec": within_spec}


class RunDataEvidenceExample:
    """Run the power-supply evidence suite as a standalone program."""

    @staticmethod
    async def run() -> RunResult:
        """Execute through the framework-owned lifecycle."""
        return await PowerSupply(
            name="power supply",
            output_voltage_v=12.1,
            temperature_c=48.0,
        ).run()


if __name__ == "__main__":
    asyncio.run(RunDataEvidenceExample.run())
