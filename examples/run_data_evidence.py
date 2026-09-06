"""The evidence surface as the subject: readings travel with the verdict.

`configure` returns the context the test methods rely on — here the spec limits.
Those entries merge into `utils.ctx`, which is read-only from then on. Each test
method reads its limit from ctx, takes its reading, and hands the reading back
with the verdict: `PASS({...})` when the unit is in spec, `TestFailure(...,
evidence={...})` when it is not. `@evidence` names the reading each pass must
carry, so a pass without it is a contract violation rather than a green run.

The leaf's record is the ctx view plus what its verdict carried: the limit sits
beside the reading. The authored node's `run_data` is the ctx view alone. A
reading a test method never took is absent from the record, never reported as
`0.0`; a declared reading that was not taken is listed by name so the Floor
Technician sees the gap.

The readings are constructor-injected so the example runs on any development
machine. A real suite would take them with `utils.exec()` inside the test
method; the declaration, the verdict, and the record stay the same.

Run it: `litmus run examples/run_data_evidence.py`.
"""

from __future__ import annotations

from typing import Any

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict, evidence


class PowerSupply(LitmusTest):
    """Judge voltage and temperature against their limits and record both readings."""

    def __init__(
        self,
        *,
        name: str,
        output_voltage_v: float = 12.1,
        temperature_c: float = 48.0,
        voltage_low_v: float = 11.5,
        voltage_high_v: float = 12.5,
        temp_limit_c: float = 70.0,
    ) -> None:
        """Capture the readings this suite will publish as evidence.

        The default readings sit inside the limits, so `litmus run` needs no
        arguments.

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
        """Provide the limits the test methods judge against; readings are theirs to carry."""
        return {
            "voltage_low_v": self._voltage_low_v,
            "voltage_high_v": self._voltage_high_v,
            "temp_limit_c": self._temp_limit_c,
        }

    def build_suite(self) -> LitmusTest:
        """Independent claims: voltage and temperature do not depend on each other."""
        return self.parallel(
            self.name,
            [
                self.test("output-voltage", self._check_voltage),
                self.test("temperature", self._check_temperature),
            ],
        )

    @evidence("output_voltage_v")
    async def _check_voltage(self, utils: TestUtils) -> Verdict:
        low: float = utils.ctx["voltage_low_v"]
        high: float = utils.ctx["voltage_high_v"]
        voltage = self._output_voltage_v
        utils.logger.info("PSU voltage: %s V (spec: %s-%s V)", voltage, low, high)
        reading = {"output_voltage_v": voltage}
        if low <= voltage <= high:
            return PASS(reading)
        raise TestFailure(
            "PSU voltage out of spec",
            f"{low:g}-{high:g} V",
            f"{voltage:g} V",
            evidence=reading,
        )

    @evidence("temperature_c")
    async def _check_temperature(self, utils: TestUtils) -> Verdict:
        limit: float = utils.ctx["temp_limit_c"]
        temperature = self._temperature_c
        utils.logger.info("PSU temperature: %s degC (limit: %s degC)", temperature, limit)
        reading = {"temperature_c": temperature}
        if temperature <= limit:
            return PASS(reading)
        raise TestFailure(
            "PSU temperature over limit",
            f"<= {limit:g} degC",
            f"{temperature:g} degC",
            evidence=reading,
        )
