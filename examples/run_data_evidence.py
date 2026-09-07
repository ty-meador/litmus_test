"""The evidence surface as the subject: readings travel with the verdict.

Concept: inputs come in through `configure()`; readings go out with the verdict.

There are two kinds of value in a run, and the framework keeps them apart on purpose:

- **Context** is what a claim needs to know before it runs: spec limits, handles,
  addresses. `configure()` returns it, it merges into `utils.ctx`, and ctx is
  read-only from then on. A claim that reads a key `configure` did not provide is
  a contract violation, so the record can never show a limit the claim did not use.
- **Evidence** is what a claim observed: the reading it judged. It is handed back
  with the verdict, `PASS({...})` on the pass path and `TestFailure(...,
  evidence={...})` on the fail path. `@evidence` names the keys a pass must carry,
  so a claim that forgets is a contract violation rather than a green run with a
  hole in it.

Why not write the reading into ctx, the way the legacy framework allowed? Because a
shared mutable bag has no owner. When two parallel claims write the same key, or one
claim overwrites a limit, the record no longer says who observed what. Tying the
reading to the verdict gives every value a node, a time, and a pass/fail it belonged
to.

The leaf's record is the ctx view plus what its verdict carried, so the limit sits
beside the reading. The authored node's `run_data` is the ctx view alone. A reading
a test method never took is absent from the record, never reported as `0.0`; a
declared reading that was not taken is listed by name so the Floor Technician sees
the gap.

The readings are constructor-injected so the example runs on any development
machine. A real suite would take them with `utils.exec()` inside the test method;
the declaration, the verdict, and the record stay the same.

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
        """Provide the limits the test methods judge against; readings are theirs to carry.

        Only the limits go in. Seeding `output_voltage_v: 0.0` here "so the key
        exists" would put a reading in the record that nobody took; if the claim
        then failed before measuring, the FT would see a plausible number instead of
        an absence. The framework rejects placeholder readings for that reason.
        """
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

    # Each claim below follows one pattern:
    #   1. read its limits from ctx (provided by `configure`, read-only here);
    #   2. take the reading;
    #   3. build the evidence mapping once, so pass and fail carry the same keys;
    #   4. gate the verdict on the reading and hand the evidence back with it.

    @evidence("output_voltage_v")
    async def _check_voltage(self, utils: TestUtils) -> Verdict:
        low: float = utils.ctx["voltage_low_v"]
        high: float = utils.ctx["voltage_high_v"]
        voltage = self._output_voltage_v
        utils.logger.info("PSU voltage: %s V (spec: %s-%s V)", voltage, low, high)
        reading = {"output_voltage_v": voltage}
        if low <= voltage <= high:
            # `PASS(reading)` mints a verdict that carries a detached copy of the
            # mapping. The boundary checks it against the `@evidence` declaration
            # and copies it into the leaf's record; the claim cannot edit it after.
            return PASS(reading)
        # The fail path carries the same reading. The FT sees the value that failed,
        # not just the limit it failed against.
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
