"""Survey and audit hooks around a device with real state transitions.

Concept: the lifecycle has boundaries, and hooks are how a suite observes them.

A `LitmusTest` runs `configure` → test phase → `cleanup`. Around those three there
are six boundaries (`pre-configure`, `post-configure`, `pre-test`, `post-test`,
`pre-cleanup`, `post-cleanup`), and two kinds of hook attach to the run:

- A **survey** fires once at each boundary and records whatever it returns as an
  artifact named for that boundary. It is the "what did the device look like at
  this moment" record. A survey error is logged and never touches the verdict,
  because a failed snapshot is not a failed device.
- An **audit** runs on an interval while claims execute. It is the "was anything
  out of bounds while we were looking elsewhere" record. An audit that raises
  `TestFailure` is a real failure: it cancels the in-flight claim and fails the
  suite, because an over-temperature during the test is a device fault regardless
  of what the claim would have concluded.

This example puts both around a device with state that actually changes. `configure`
brings a power rail up and waits for it, the survey snapshots the rail at every
boundary through the rail's own handle (so the post-cleanup snapshot shows it down),
the audit reads a thermal feed during the claim, and `cleanup` drops the rail.

Ctx is read-only after `configure` returns. That is not a limitation here: the
things that change during the run are objects that manage themselves (the rail, the
thermal feed), and the reading the Floor Technician needs travels with the verdict.
Ctx holds handles; evidence holds readings.

The observations are scripted so the example runs on any development machine.
Replace the constructor values with `utils.exec()` reads in a real suite; the hook
registration, the ctx flow, and the evidence shape stay the same.

Run it: `litmus run examples/hardware_device_lifecycle.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import TypedDict, cast

from litmus import PASS, LitmusTest, TestFailure, TestUtils, Verdict, evidence


class _ScriptedRail:
    """A rail whose state the suite reads and drops; stands in for a real controller.

    The point of a handle object in ctx is that it owns its own state. Ctx cannot be
    written after `configure`, but a handle can change what it reports, and the
    survey reads the current state through it rather than through a ctx key that
    would be frozen at "on".
    """

    def __init__(self, states: tuple[str, ...]) -> None:
        self._states = iter(states)
        self.state = "not-observed"

    def poll(self) -> str:
        """Advance the scripted ramp and return the current state."""
        # `next(..., self.state)` holds the last state once the script runs out, the
        # way a real rail stays "on" after it finishes ramping.
        self.state = next(self._states, self.state)
        return self.state

    def drop(self) -> None:
        """Turn the rail off."""
        self.state = "off"


def _repeat_last(readings: tuple[float, ...]) -> Iterator[float]:
    """Yield each scripted reading, then the last one forever; empty means 0.0.

    The audit samples on a timer, and the number of samples depends on how long the
    claim holds. An iterator that never runs dry means the sample count can vary
    without the audit hitting `StopIteration` mid-run.
    """
    last = 0.0
    for reading in readings:
        last = reading
        yield reading
    while True:
        yield last


class _DeviceContext(TypedDict):
    """What `configure` provides: the handles the hooks and the claim work through.

    Naming the ctx shape as a `TypedDict` lets pyright check every `ctx[...]` read in
    the hooks against what `configure` promised, so a renamed key is a type error at
    edit time rather than a contract violation at run time.
    """

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
        # Hooks are registered in the constructor, not in `configure`, because they
        # are part of the suite's identity: the same boundaries are observed and the
        # same feed is audited on every run. Registration after construction is a
        # contract violation, which keeps a run from quietly gaining or losing an
        # observer depending on which code path `configure` took.
        self.use_survey(self._snapshot)
        self.use_audit(interval=audit_interval, fn=self._thermals)

    async def configure(self, utils: TestUtils) -> _DeviceContext:
        """Block until the rail is on and provide the handles the run works through.

        A `wait_for` in `configure` means "the device is not testable until this is
        true". If the rail never reaches "on", the timeout is a Fail from configure
        with `looking_for` as the message, and no claim runs against a dead board.
        """
        rail = _ScriptedRail(self._rail_states)

        async def rail_is_on() -> bool:
            # The `sleep(0)` yields to the loop so a scripted poll behaves like an
            # asynchronous hardware read and cannot starve the audit or the surveys.
            await asyncio.sleep(0)
            utils.logger.info("rail state: %s", rail.poll())
            return rail.state == "on"

        await utils.wait_for(
            rail_is_on,
            looking_for="power rail on",
            interval=self._interval,
            timeout_seconds=self._timeout_seconds,
        )
        # The rail handle and the thermal iterator are not JSON, so the run record
        # strips them from the ctx view; only `limit_c` shows in `run_data`. That is
        # the intended split: handles are for the run, readings are for the record.
        return {
            "rail": rail,
            "thermals": _repeat_last(self._temperatures),
            "limit_c": self._limit_c,
        }

    def build_suite(self) -> LitmusTest:
        """Claim the device enumerated only after the rail is up."""
        return self.test("device-enumerates", self._device_enumerates)

    async def cleanup(self, utils: TestUtils) -> None:
        """Drop the rail. Always runs; must not throw.

        Cleanup runs whether the claim passed, failed, or was cancelled by the audit,
        and after a failed `configure` as well. The `isinstance` guard is there for
        that last case: if `configure` raised before returning, the rail key was
        never provided and `ctx.get` is `None`.
        """
        rail = utils.ctx.get("rail")
        if isinstance(rail, _ScriptedRail):
            rail.drop()
            utils.logger.info("rail dropped")

    async def _snapshot(self, utils: TestUtils) -> dict[str, object]:
        """Capture the rail state at each boundary, from the handle itself.

        This fires six times. At `pre-configure` there is no rail yet and the
        snapshot says "unknown"; at `post-cleanup` the handle says "off". Reading
        through the handle is what lets one hook tell the truth at every boundary.
        """
        rail = utils.ctx.get("rail")
        return {"rail": rail.state if isinstance(rail, _ScriptedRail) else "unknown"}

    async def _thermals(self, utils: TestUtils) -> dict[str, object]:
        """Sample temperature. A reading over the limit is a real Fail.

        The audit does not get a `@evidence` declaration or a verdict; its return
        value is an artifact each time it fires, and its `TestFailure` is the
        suite's failure. Raising here cancels the in-flight claim: an enumeration
        that completed while the board was over its limit is not a pass.
        """
        # The cast is safe because `configure` returned a `_DeviceContext`; it gives
        # the two reads below their real types without repeating `isinstance` guards.
        ctx = cast("_DeviceContext", utils.ctx)
        reading = next(ctx["thermals"])
        limit = ctx["limit_c"]
        if reading > limit:
            raise TestFailure("temperature over limit", f"<= {limit:g} degC", f"{reading:g} degC")
        return {"temperature_c": reading, "limit_c": limit}

    @evidence("enumerated")
    async def _device_enumerates(self, utils: TestUtils) -> Verdict:
        # Holding the claim open for a moment is what gives the audit time to sample
        # during it; a real enumeration check would spend this time on the bus.
        await asyncio.sleep(self._claim_seconds)
        if self._enumerated:
            utils.logger.info("device enumerated")
            return PASS({"enumerated": True})
        # `expected` and `received` are the booleans the claim compared; the evidence
        # mapping repeats the reading under its declared name so the record and the
        # failure agree.
        raise TestFailure("device did not enumerate", True, False, evidence={"enumerated": False})
