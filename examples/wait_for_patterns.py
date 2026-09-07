"""Polling patterns and the evidence trail they produce.

Concept: where `wait_for()` is called determines what its timeout means.

`utils.wait_for(predicate, looking_for=..., interval=..., timeout_seconds=...)` polls
until the predicate is true or the window closes. The call is the same in both
places below; the meaning of running out of time is not:

- In `configure()`, a timeout means the device never reached a testable state. The
  suite Fails from configure, no claim runs, and `looking_for` is the message. This
  is the readiness gate: "do not judge a service that has not finished starting".
- In a test method, the polling window *is* the claim. "The stress marker appears
  within the budget" is a statement about the hardware, and a timeout is the
  hardware verdict.

In both places a timeout is a `TestFailure` raised by `wait_for` itself, with
`looking_for` as the expected value and `"timeout"` as the received one. The suite
does not write that failure; it writes the predicate and the words.

In both places `looking_for` is required and written in plain language, because it
becomes the Floor Technician's primary diagnostic when the wait times out. "my-service
active" tells them what was being waited for; a stack trace does not.

The example also shows the evidence trail a polling claim leaves without any extra
API: scoped log lines per poll, a point-in-time `Artifact` at the moment of success,
readiness facts returned into ctx from `configure`, and the reading handed back on
the verdict with `PASS({...})` or `TestFailure(..., evidence={...})`.

The observations below are scripted so the example runs on any development machine.
Replace them with `utils.exec()` or another asynchronous hardware read in a real
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
        # The survey fires at all six boundaries. It reads only ctx, never the
        # device, so there is no need to schedule it from post-configure onward the
        # way a device-reading survey must be (compare `pi_hole_server.py`).
        self.use_survey(self._service_snapshot)

    async def configure(self, utils: TestUtils) -> dict[str, Any]:
        """Block until the service is active and return the readiness evidence.

        The predicate is a closure over the poll counter and the last state seen.
        `wait_for` only learns true or false from it, so the closure is where the
        observations are kept for the log, the artifact, and the ctx return.
        """
        states = iter(self._service_states)
        service_state = "not-observed"
        attempts = 0

        async def service_is_ready() -> bool:
            nonlocal attempts, service_state
            # Yielding once per poll is what a real asynchronous read would do; it
            # keeps a scripted predicate from spinning the loop.
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
            # A point-in-time artifact marks the moment readiness was observed. The
            # log lines say the same thing, but an artifact is addressable by name
            # in the run directory, which is what a downstream tool wants.
            await utils.capture_artifact(
                Artifact(
                    name="service-ready",
                    type="polling-observation",
                    data={"attempts": attempts, "state": service_state},
                )
            )
        # These are facts about readiness, known before any claim runs. They belong
        # in ctx, where they land on the suite's `run_data`, rather than on the
        # health claim's verdict, which did not observe them.
        return {"service_state": service_state, "service_poll_attempts": attempts}

    def build_suite(self) -> LitmusTest:
        """Run the health claim only after the configure gate succeeds."""
        return self.test("service-health", self._check_health)

    async def cleanup(self, utils: TestUtils) -> None:
        """Leave the service as it was found."""
        utils.logger.info("service cleanup complete")

    async def _service_snapshot(self, utils: TestUtils) -> dict[str, object]:
        """Capture the readiness state visible at each lifecycle phase boundary.

        At `pre-configure` neither key exists yet, so the defaults say so plainly
        ("not-configured", 0). A survey that raised `KeyError` there would be
        swallowed and leave no artifact; a survey that reports the absence leaves
        a record the FT can read in order.
        """
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
        """Provide the polling budget the claim reads.

        The budget is part of the claim's specification ("appears within N seconds"),
        so it goes through ctx like any other limit. That puts it on the record next
        to the attempt count, and the ctx-sufficiency rule guarantees the claim
        cannot read a budget `configure` did not state.
        """
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

        # Same call as the configure gate; the difference is that a timeout here is
        # the verdict, not a bench problem. `wait_for` raises that `TestFailure`
        # itself, and the per-poll log lines above carry the attempt count to the
        # FT on that path.
        observed = await utils.wait_for(
            stress_is_complete,
            looking_for="stress completion marker",
            interval=utils.ctx["poll_interval_s"],
            timeout_seconds=utils.ctx["poll_timeout_s"],
        )
        # Both declared keys are built before the gate so pass and fail carry the
        # same shape. `stress_poll_attempts` tells the FT how many looks the marker
        # took, which is the number they need to tune the budget.
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
