"""Sequence and parallel composition translated from the legacy Litmus example.

The values are deterministic stand-ins for device readings. This keeps the example
dependent claims belong in a sequence; independent claims belong in a parallel node.

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
        """
        super().__init__(name=name)
        self._service_active = service_active
        self._firmware_version = firmware_version
        self._application_pid = application_pid

    def build_suite(self) -> LitmusTest:
        """Order the startup claims so later checks depend on earlier success."""
        return self.sequence(
            self.name,
            [
                self.test("boot-check", self._boot_check),
                self.test("firmware-version", self._firmware_version_check),
                self.test("application-running", self._application_running),
            ],
        )

    async def _boot_check(self, _utils: TestUtils) -> Verdict:
        if self._service_active:
            return PASS
        raise TestFailure("service not active", "active", "inactive")

    async def _firmware_version_check(self, _utils: TestUtils) -> Verdict:
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
        parallel node collects both failures instead of stopping at the first.
        """
        super().__init__(name=name)
        self._temperature_c = temperature_c
        self._voltage_v = voltage_v
        self._fan_rpm = fan_rpm

    def build_suite(self) -> LitmusTest:
        """Group the sensor claims because none depends on another."""
        return self.parallel(
            self.name,
            [
                self.test("temperature", self._temperature),
                self.test("voltage", self._voltage),
                self.test("fan-speed", self._fan_speed),
            ],
        )

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
