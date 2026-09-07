"""Sequence and parallel composition translated from the legacy Litmus example.

Concept: the shape of the tree is a statement about dependencies between claims.

- A `sequence` says "each claim assumes the ones before it passed". It stops at the
  first failure, and the tree records the claims it never reached as pending rather
  than pretending they passed. Use it when a later claim would be meaningless, or
  misleading, after an earlier one failed.
- A `parallel` says "these claims do not depend on each other". Every claim runs and
  every result is recorded, so one run shows the Floor Technician all of the
  failures instead of the first one.

Both nodes have verdicts of their own: a sequence or parallel is Pass only when every
child is. The verdict of the composition is what `litmus run` reports and what the
exit status reflects.

The values are deterministic stand-ins for device readings so the example runs on
any development machine. A real suite takes them with `utils.exec()` or a hardware
read inside the test method; the composition is unchanged.

Run them: `litmus run examples/test_composition.py::SequencedDevice` settles Pass;
`litmus run examples/test_composition.py::ParallelDevice` settles Fail with two of
its three claims recorded, and exits 1.
"""

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict


class SequencedDevice(LitmusTest):
    """Check dependent startup claims serially and stop at the first failure."""

    def __init__(
        self,
        *,
        name: str,
        service_active: bool = True,
        firmware_version: str = "2.4.1",
        application_pid: int | None = 412,
    ) -> None:
        """Capture the simulated observations that the sequence will evaluate.

        The defaults are the healthy readings, so `litmus run` needs no arguments.
        The self-tests pass unhealthy ones to pin the stop-at-first-failure rule.
        """
        super().__init__(name=name)
        self._service_active = service_active
        self._firmware_version = firmware_version
        self._application_pid = application_pid

    def build_suite(self) -> LitmusTest:
        """Order the startup claims so later checks depend on earlier success.

        Read the list top to bottom as "boot, then firmware, then the application".
        If the service never came up, the firmware string is whatever the last flash
        left behind and the PID is stale; reporting either would send the FT after
        the wrong fault. The sequence encodes that reasoning so it holds on every run.
        """
        return self.sequence(
            self.name,
            [
                self.test("boot-check", self._boot_check),
                self.test("firmware-version", self._firmware_version_check),
                self.test("application-running", self._application_running),
            ],
        )

    # Each claim below has the same three-line shape: the gate, the pass, the fail
    # with expected and received. `_utils` is underscored because these scripted
    # claims take no reading; a real one would call `utils.exec()` here.

    async def _boot_check(self, _utils: TestUtils) -> Verdict:
        if self._service_active:
            return PASS
        raise TestFailure("service not active", "active", "inactive")

    async def _firmware_version_check(self, _utils: TestUtils) -> Verdict:
        # Substring, not equality: a build string such as "release-2.4.1" still
        # carries the version the claim is about.
        if "2.4.1" in self._firmware_version:
            return PASS
        raise TestFailure("firmware version mismatch", "2.4.1", self._firmware_version)

    async def _application_running(self, _utils: TestUtils) -> Verdict:
        if self._application_pid is not None:
            return PASS
        raise TestFailure("application not running", "pid present", "(none)")


class ParallelDevice(LitmusTest):
    """Check independent sensor claims concurrently and collect every failure."""

    def __init__(
        self,
        *,
        name: str,
        temperature_c: float = 92.0,
        voltage_v: float = 3.3,
        fan_rpm: int = 800,
    ) -> None:
        """Capture the simulated readings that the parallel node will evaluate.

        The defaults put temperature and fan speed out of spec on purpose: the
        parallel node collects both failures instead of stopping at the first, and
        the example exists to show that. Running it is expected to exit 1.
        """
        super().__init__(name=name)
        self._temperature_c = temperature_c
        self._voltage_v = voltage_v
        self._fan_rpm = fan_rpm

    def build_suite(self) -> LitmusTest:
        """Group the sensor claims because none depends on another.

        A hot board does not make the voltage reading wrong, and a slow fan does not
        make the temperature reading wrong. Running them in parallel means one run
        of the suite reports the whole picture; a sequence here would hide the fan
        failure behind the temperature failure and cost the FT a second run.
        """
        return self.parallel(
            self.name,
            [
                self.test("temperature", self._temperature),
                self.test("voltage", self._voltage),
                self.test("fan-speed", self._fan_speed),
            ],
        )

    # The `expected` and `received` strings carry units and the formatted value.
    # They are shown side by side in the failure record, so writing them for a human
    # ("<= 85 degC" against "92 degC") is worth more than the raw float.

    async def _temperature(self, _utils: TestUtils) -> Verdict:
        if self._temperature_c <= 85:
            return PASS
        raise TestFailure("temperature over limit", "<= 85 degC", f"{self._temperature_c:g} degC")

    async def _voltage(self, _utils: TestUtils) -> Verdict:
        if abs(self._voltage_v - 3.3) <= 0.1:
            return PASS
        raise TestFailure("voltage out of spec", "3.3 +/- 0.1 V", f"{self._voltage_v:.3f} V")

    async def _fan_speed(self, _utils: TestUtils) -> Verdict:
        if self._fan_rpm >= 1000:
            return PASS
        raise TestFailure("fan speed below minimum", ">= 1000 RPM", f"{self._fan_rpm} RPM")
