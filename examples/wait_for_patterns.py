"""Polling patterns and the evidence trail they produce.

Where ``wait_for()`` is called determines what its timeout means. A timeout in
``configure()`` means the device never reached a testable state; a timeout in a test
method is the hardware verdict itself. In both places, ``looking_for`` is the primary
diagnostic shown to the Floor Technician.

Readiness evidence from ``configure()`` is returned into ctx; the reading a test
method takes travels with its verdict, ``PASS({...})`` or ``TestFailure(...,
evidence={...})``, and ``@evidence`` names what a pass must carry.

The observations below are scripted so the example runs on any development machine.
Replace them with ``utils.exec()`` or another asynchronous hardware read in a real
suite; the lifecycle and evidence shape remain the same.

Run them: `litmus run examples/wait_for_patterns.py::ConfigureGatedDevice` and
`litmus run examples/wait_for_patterns.py::MainAssertionDevice`.
"""

import asyncio
from typing import Any

from litmus import PASS, Artifact, LitmusTest, TestFailure, TestUtils, Verdict, evidence


class ConfigureGatedDevice(LitmusTest):
    """Wait for readiness before allowing any hardware claim to run."""

    def __init__(
        self,
        *,
        name: str,
        service_states: tuple[str, ...] = ("activating", "active"),
        health_response: str = "ok",
        interval: float = 0.01,
        timeout_seconds: float = 1.0,
    ) -> None:
        """Capture deterministic stand-ins for the device observations.

        The defaults reach ``active`` on the second poll and report healthy, so
        `litmus run` needs no arguments.
        """
        super().__init__(name=name)
        self._service_states = service_states
        self._health_response = health_response
        self._interval = interval
        self._timeout_seconds = timeout_seconds
        self.use_survey(self._service_snapshot)

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Block until the service is active and return the readiness evidence."""
        states = iter(self._service_states)
        service_state = "not-observed"
        attempts = 0

        async def service_is_ready() -> bool:
            nonlocal attempts, service_state
            await asyncio.sleep(0)
            attempts += 1
            service_state = next(states, service_state)
            utils.logger.info("service readiness poll %d: %s", attempts, service_state)
            return service_state == "active"

        ready = await utils.wait_for(
            service_is_ready,
            looking_for="my-service active",
            interval=self._interval,
            timeout_seconds=self._timeout_seconds,
        )
        if ready:
            await utils.capture_artifact(
                Artifact(
                    name="service-ready",
                    type="polling-observation",
                    data={"attempts": attempts, "state": service_state},
                )
            )
        return {"service_state": service_state, "service_poll_attempts": attempts}

    def build_suite(self) -> LitmusTest:
        """Run the health claim only after the configure gate succeeds."""
        return self.test("service-health", self._check_health)

    async def cleanup(self, utils: TestUtils) -> None:
        """Leave the service as it was found."""
        utils.logger.info("service cleanup complete")

    async def _service_snapshot(self, utils: TestUtils) -> dict[str, object]:
        """Capture the readiness state visible at each lifecycle phase boundary."""
        return {
            "service_state": str(utils.ctx.get("service_state", "not-configured")),
            "service_poll_attempts": int(utils.ctx.get("service_poll_attempts", 0)),
        }

    @evidence("health_response")
    async def _check_health(self, utils: TestUtils) -> Verdict:
        response = self._health_response
        utils.logger.info("service health response: %s", response)
        await utils.capture_artifact(
            Artifact(
                name="service-health",
                type="polling-observation",
                data={"response": response},
            )
        )
        if "ok" in response:
            return PASS({"health_response": response})
        raise TestFailure(
            "health check failed", "ok", response, evidence={"health_response": response}
        )


class MainAssertionDevice(LitmusTest):
    """Use the polling window itself as a hardware claim."""

    def __init__(
        self,
        *,
        name: str,
        stress_states: tuple[bool, ...] = (False, False, True),
        interval: float = 0.01,
        timeout_seconds: float = 1.0,
    ) -> None:
        """Capture deterministic stress-marker observations.

        The default marker appears on the third poll, so `litmus run` needs no
        arguments.
        """
        super().__init__(name=name)
        self._stress_states = stress_states
        self._interval = interval
        self._timeout_seconds = timeout_seconds

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Provide the polling budget the claim reads."""
        return {"poll_interval_s": self._interval, "poll_timeout_s": self._timeout_seconds}

    def build_suite(self) -> LitmusTest:
        """Declare the stress-completion claim."""
        return self.test("stress-test-completes", self._wait_for_stress)

    @evidence("stress_complete", "stress_poll_attempts")
    async def _wait_for_stress(self, utils: TestUtils) -> Verdict:
        states = iter(self._stress_states)
        attempts = 0
        complete = False

        async def stress_is_complete() -> bool:
            nonlocal attempts, complete
            await asyncio.sleep(0)
            attempts += 1
            complete = next(states, complete)
            utils.logger.info("stress completion poll %d: %s", attempts, complete)
            return complete

        observed = await utils.wait_for(
            stress_is_complete,
            looking_for="stress completion marker",
            interval=utils.ctx["poll_interval_s"],
            timeout_seconds=utils.ctx["poll_timeout_s"],
        )
        reading = {"stress_complete": complete, "stress_poll_attempts": attempts}
        if observed and complete:
            await utils.capture_artifact(
                Artifact(
                    name="stress-complete",
                    type="polling-observation",
                    data={"attempts": attempts},
                )
            )
            return PASS(reading)
        raise TestFailure("stress completion marker was not observed", evidence=reading)
