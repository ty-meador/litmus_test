"""The child-boundary wrapper — the kernel of trust.

Every call litmus makes into suite code goes through `ChildBoundary.run()`. It is the
single most important function in the codebase and, by design, the most tested. It is
three things at once:

1. **Verdict enforcer.** For a test method the return path splits exactly two ways:
   `result is PASS`, or everything else. There is no third bucket.
2. **Fail / Bug / Abort classifier.** `TestFailure` becomes Fail (data); a non-PASS
   return becomes node-local Aborted (no verdict exists, so the framework forges none);
   any other exception becomes a BUG-signaled Fail; `ExternalIntegrationFailure` and an
   abort-driven `CancelledError` become Aborted (control flow).
3. **The reason TaskGroup semantics are correct.** Fail and node-local Aborted are
   *returned*, never raised — they cannot escape into the enclosing `asyncio.TaskGroup`
   and cancel seventy healthy siblings. Only a harness-level halt is allowed to
   propagate, and TaskGroup cancelling everything is then exactly right, because that
   is what Aborted means.

The classification is uniform across every lifecycle slot; what varies between slots is
policy — whether a verdict is demanded, whether errors are swallowed to protect the
hardware verdict, whether a throw is a run-halting integration failure. That policy is
declared once, in `SLOT_POLICIES`, and read nowhere else.
"""

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import StrEnum
from typing import final

from litmus.errors import (
    ContractViolation,
    ExternalIntegrationFailure,
    Failure,
    FrameworkContractViolation,
    HookName,
    MissingVerdict,
    TestFailure,
)
from litmus.verdict import PASS

LOGGER = logging.getLogger("litmus")


class State(StrEnum):
    """The three terminal states. Peers — Aborted is not a flavor of failure.

    There is no fourth. A call that settles without producing a terminal state (a
    successful `configure`, a completed `cleanup`) reports `Outcome.state is None`,
    which says "nothing terminal happened here" rather than inventing a state to say it.
    """

    PASS = "Pass"
    FAIL = "Fail"
    ABORTED = "Aborted"


class Slot(StrEnum):
    """The lifecycle positions the framework calls suite code from.

    The slot is what the boundary needs to know to apply the right policy; it is also
    what the FT reads in the log to know which of their functions misbehaved.
    """

    TEST_METHOD = "test method"
    CONFIGURE = "configure"
    CLEANUP = "cleanup"
    SURVEY = "survey"
    AUDIT = "audit"
    BEFORE_EACH = "before_each"
    AFTER_EACH = "after_each"


@final
@dataclass(frozen=True, slots=True)
class SlotPolicy:
    """How one lifecycle slot differs from the others. Declared once, read once."""

    demands_verdict: bool
    """The return value must be the PASS sentinel. True for test methods alone —
    `configure` returns ctx, hooks and cleanup return nothing meaningful."""

    swallows_errors: bool
    """Errors are logged and discarded rather than recorded as a verdict. True for
    cleanup and survey: evidence collection must never mask the hardware verdict. An
    `ExternalIntegrationFailure` is exempt — swallowing it would kill the halt signal."""

    halts_run_on_error: bool
    """A throw is an integration failure: wrap it, fire the run abort, propagate. True
    for the `before_each` / `after_each` hooks, whose failure means the shop-floor
    system is broken and no verdict can be trusted."""


SLOT_POLICIES: dict[Slot, SlotPolicy] = {
    # The whole of the per-slot difference. Read only by `ChildBoundary._policy_for`.
    Slot.TEST_METHOD: SlotPolicy(
        demands_verdict=True, swallows_errors=False, halts_run_on_error=False
    ),
    Slot.CONFIGURE: SlotPolicy(
        demands_verdict=False, swallows_errors=False, halts_run_on_error=False
    ),
    Slot.CLEANUP: SlotPolicy(demands_verdict=False, swallows_errors=True, halts_run_on_error=False),
    Slot.SURVEY: SlotPolicy(demands_verdict=False, swallows_errors=True, halts_run_on_error=False),
    Slot.AUDIT: SlotPolicy(demands_verdict=False, swallows_errors=False, halts_run_on_error=False),
    Slot.BEFORE_EACH: SlotPolicy(
        demands_verdict=False, swallows_errors=False, halts_run_on_error=True
    ),
    Slot.AFTER_EACH: SlotPolicy(
        demands_verdict=False, swallows_errors=False, halts_run_on_error=True
    ),
}


@final
@dataclass(frozen=True, slots=True)
class Outcome:
    """What one call through the boundary amounted to.

    `state` is `None` when the call settled without producing a terminal state — a
    `configure` that returned its ctx, a `cleanup` that finished. That is not a fourth
    state; it is the absence of one.
    """

    slot: Slot
    node: str
    state: State | None
    failure: Failure | None = None
    value: object = None
    """The raw return value, kept for slots that do not demand a verdict: configure's
    ctx dict, a survey hook's artifact payload."""

    swallowed: BaseException | None = None
    """An error from cleanup or survey that was logged and discarded so it could not
    mask the hardware verdict."""

    @property
    def is_bug(self) -> bool:
        """True if this outcome was caused by broken code rather than broken hardware."""
        return self.failure is not None and self.failure.is_bug


@final
class RunAbort:
    """The run-level halt signal.

    Phase 0 carries only what the child boundary needs: whether a halt is in flight and
    why. That is enough to tell an abort-driven `CancelledError` — a legitimate Aborted
    — from a spurious one, which is a framework bug. The race machinery and transport
    hard-close that ride this signal arrive in Phase 4.
    """

    def __init__(self) -> None:
        self._reason: ExternalIntegrationFailure | None = None

    @property
    def fired(self) -> bool:
        """True once a halt has been signaled. One-way; a run never un-aborts."""
        return self._reason is not None

    @property
    def reason(self) -> ExternalIntegrationFailure | None:
        """Why the run was halted, or `None` if it was not."""
        return self._reason

    def fire(self, reason: ExternalIntegrationFailure) -> None:
        """Signal the halt. Idempotent: the first reason wins.

        The first failure is the one that explains the run; a later hook failing during
        the resulting teardown must not overwrite it.
        """
        if self._reason is None:
            self._reason = reason


@final
class ChildBoundary:
    """The wrapper around every call into suite code, for one node.

    Contract:
      - Returns an `Outcome` when the call settled node-locally — Pass, Fail, node-local
        Aborted, or nothing terminal. Nothing is raised, so nothing reaches the
        enclosing `TaskGroup` and no sibling is disturbed.
      - Raises when the halt is harness-level: `ExternalIntegrationFailure`,
        `CancelledError`, or any other `BaseException` outside the taxonomy. The
        outcome is recorded on `self.outcome` **before** the exception leaves, so an
        aborted node still has its story in the artifact.
      - Never swallows a cancellation. Structured concurrency depends on
        `CancelledError` continuing outward; the boundary classifies it and re-raises.
    """

    def __init__(
        self,
        node: str,
        run_abort: RunAbort | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Args:
        node: The node's name, as it appears in logs and contract-violation banners.
        run_abort: The run-level halt signal. A private one is made if omitted, which
            is what a standalone bench run of a single node wants.
        logger: Where swallowed errors and bug banners go. Defaults to `litmus`.
        """
        self._node = node
        self._run_abort = run_abort if run_abort is not None else RunAbort()
        self._logger = logger if logger is not None else LOGGER
        self._outcome: Outcome | None = None

    @property
    def node(self) -> str:
        """The name of the node this boundary guards."""
        return self._node

    @property
    def run_abort(self) -> RunAbort:
        """The halt signal this boundary consults and, from a hook slot, fires."""
        return self._run_abort

    @property
    def outcome(self) -> Outcome | None:
        """The last classification, including for calls that raised. `None` before use."""
        return self._outcome

    async def run(self, slot: Slot, call: Awaitable[object]) -> Outcome:
        """Await suite code and classify what came back.

        Args:
            slot: Which lifecycle position `call` occupies. Decides policy.
            call: The awaitable returned by the suite's function.

        Returns:
            The outcome, for every node-local settlement.

        Raises:
            ExternalIntegrationFailure: A hook threw, or one arrived from below.
            asyncio.CancelledError: The run is being torn down.
            BaseException: A halt outside the taxonomy — `KeyboardInterrupt`,
                `SystemExit` — passed through untouched.
        """
        policy = self._policy_for(slot)
        try:
            result = await call
        except ExternalIntegrationFailure as failure:
            # Never rewrapped and never swallowed: the halt signal outranks every slot
            # policy, and the integrator must see their own error class intact.
            self._run_abort.fire(failure)
            self._record(Outcome(slot, self._node, State.ABORTED, failure=failure))
            raise
        except asyncio.CancelledError:
            self._record(self._classify_cancellation(slot))
            raise
        except TestFailure as failure:
            if policy.halts_run_on_error:
                raise self._as_integration_failure(slot, failure) from failure
            return self._record(self._classify_thrown(slot, policy, failure))
        except Exception as error:
            if policy.halts_run_on_error:
                raise self._as_integration_failure(slot, error) from error
            # Suite code raised something that is not a verdict. It lands in Fail, but
            # banners itself so nobody mistakes a broken test for a broken unit.
            violation = ContractViolation(self._node, cause=error)
            self._logger.error("%s", violation.fail_message)
            return self._record(self._classify_thrown(slot, policy, violation))
        else:
            return self._record(self._classify_return(slot, policy, result))

    def _policy_for(self, slot: Slot) -> SlotPolicy:
        policy = SLOT_POLICIES.get(slot)
        if policy is None:
            # Second guard layer: a slot with no policy means litmus itself is wrong.
            violation = FrameworkContractViolation(
                self._node, detail=f"no policy declared for slot {slot!r}"
            )
            self._logger.error("%s", violation.fail_message)
            raise violation
        return policy

    def _classify_return(self, slot: Slot, policy: SlotPolicy, result: object) -> Outcome:
        if not policy.demands_verdict:
            return Outcome(slot, self._node, None, value=result)
        if result is PASS:
            return Outcome(slot, self._node, State.PASS)
        # The split is exactly `is PASS` versus everything else. A `None` fall-through
        # and a forged `"PASS"` string land here together, and neither becomes a Fail:
        # no verdict was produced, so the framework reports none.
        missing = MissingVerdict(self._node, returned=result)
        self._logger.error("%s", missing.fail_message)
        return Outcome(slot, self._node, State.ABORTED, failure=missing, value=result)

    def _classify_thrown(self, slot: Slot, policy: SlotPolicy, failure: TestFailure) -> Outcome:
        """Record a Fail — or discard it, where the slot exists to gather evidence."""
        if policy.swallows_errors:
            self._logger.error(
                "%s failed (verdict unaffected): %s", slot.value, failure.fail_message
            )
            return Outcome(slot, self._node, None, swallowed=failure)
        return Outcome(slot, self._node, State.FAIL, failure=failure)

    def _classify_cancellation(self, slot: Slot) -> Outcome:
        reason = self._run_abort.reason
        if reason is not None:
            return Outcome(slot, self._node, State.ABORTED, failure=reason)
        # Cancelled with no halt in flight. The node is still Aborted — it produced no
        # verdict — but nothing in litmus asked for this, so the framework layer of the
        # bug guard fires.
        violation = FrameworkContractViolation(
            self._node, detail=f"cancellation arrived in {slot.value} with no run abort in flight"
        )
        self._logger.error("%s", violation.fail_message)
        return Outcome(slot, self._node, State.ABORTED, failure=violation)

    def _as_integration_failure(
        self, slot: Slot, cause: BaseException
    ) -> ExternalIntegrationFailure:
        hook: HookName = "before_each" if slot is Slot.BEFORE_EACH else "after_each"
        failure = ExternalIntegrationFailure(hook, cause)
        self._logger.error("%s", failure.fail_message)
        self._run_abort.fire(failure)
        self._record(Outcome(slot, self._node, State.ABORTED, failure=failure))
        return failure

    def _record(self, outcome: Outcome) -> Outcome:
        self._outcome = outcome
        return outcome
