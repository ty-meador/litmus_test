"""The failure taxonomy.

Three peer terminal states — Pass, Fail, Aborted — and the error types that carry the
latter two. The distinction between them is load-bearing, not cosmetic:

  - `TestFailure` is **data**. It is a real verdict of Fail about the hardware. The
    child boundary catches it and records it; a failing DUT never becomes control flow.
  - `ExternalIntegrationFailure` is **control flow**. It is a harness-level halt raised
    when an integration hook throws. It propagates class-intact through every ancestor,
    is never rewrapped, and cancels the run.
  - `MissingVerdict` is **the absence of data**. The node returned something other than
    the PASS sentinel, so no verdict exists. The framework refuses to forge one; the
    node renders Aborted and its siblings are untouched.

`ContractViolation` and `FrameworkContractViolation` are the two layers of the bug
guard: a developer bug and a framework bug, each with its own banner so the reader of
a log can tell instantly whose code is wrong.
"""

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Literal, cast, final


@final
class _Missing:
    """Sentinel for "no value was supplied" — distinct from a supplied `None`.

    A test may legitimately expect or receive `None`; `MISSING` is how the taxonomy
    tells "no comparison was given" from "the comparison value is None".
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<missing>"


MISSING = _Missing()


def _is_json_serializable(value: object) -> bool:
    """True if `value` is a primitive, or a list/dict built only of primitives."""
    if value is None or isinstance(value, str | int | float | bool):
        return True
    if isinstance(value, list | tuple):
        return all(_is_json_serializable(item) for item in cast("Sequence[object]", value))
    if isinstance(value, dict):
        items = cast("Mapping[object, object]", value).items()
        return all(isinstance(key, str) and _is_json_serializable(item) for key, item in items)
    return False


def _normalize_json(value: object) -> Any:
    if isinstance(value, list | tuple):
        return [_normalize_json(item) for item in cast("Sequence[object]", value)]
    if isinstance(value, dict):
        items = cast("Mapping[str, object]", value).items()
        return {key: _normalize_json(item) for key, item in items}
    return value


def _json_safe(value: object) -> Any:
    """Return a JSON-safe rendering of `value`. Never raises.

    Failures carry `expected` / `received` / `cause` values that came from suite code
    and may hold live handles — SSH interfaces, streams, open files. Those must never
    reach an artifact: this collapses them to something a reader can still use rather
    than letting serialization throw and mask the failure entirely.

    Branches are ordered by specificity: framework failures recurse through their
    own `to_json()` so nested structure survives; a plain exception is extracted by
    hand because `BaseException` has no JSON form; anything already serializable
    passes through; everything else degrades to `str()`.
    """
    if isinstance(value, TestFailure | ExternalIntegrationFailure | MissingVerdict):
        return value.to_json()
    if isinstance(value, BaseException):
        return {"name": type(value).__name__, "message": str(value)}
    if _is_json_serializable(value):
        return _normalize_json(value)
    return str(value)


class LitmusError(Exception):
    """Root of the framework's own exception taxonomy.

    Catching this catches everything litmus raises deliberately and nothing a suite
    author raises by accident.
    """


class TestFailure(LitmusError):
    """A verdict of Fail: the hardware did not meet the spec.

    Raised by suite code. Caught and recorded at the child boundary, which is what
    makes a failing DUT data rather than control flow — server 12 failing POST does
    not disturb the other 69.

    Raised from `configure()` it means "did not reach a testable state". Whether the
    cause was hardware or infrastructure is not knowable from code; the human triager
    decides and the framework does not pretend otherwise.
    """

    __test__ = False
    """Not a pytest test class. This framework's domain vocabulary collides with
    pytest's collection prefix; every public `Test*` class carries this so a suite
    importing one does not trip a collection warning. Inherited by subclasses."""

    def __init__(
        self,
        fail_message: str,
        expected: object = MISSING,
        received: object = MISSING,
    ) -> None:
        """Args:
        fail_message: What failed, in the FT's language.
        expected: The spec-defined value, when there is one.
        received: The value actually observed — unknown, which is why we test.
        """
        super().__init__(fail_message)
        self.fail_message = fail_message
        self.expected = expected
        self.received = received

    @property
    def has_comparison(self) -> bool:
        """True if this failure carries structured expected/received values.

        Check before rendering the comparison, so a failure that is self-evident does
        not print two empty columns.
        """
        return self.expected is not MISSING or self.received is not MISSING

    @property
    def is_bug(self) -> bool:
        """True if this Fail was caused by broken code rather than broken hardware."""
        return False

    def to_json(self) -> dict[str, Any]:
        """JSON-safe snapshot for the run artifact. Never raises."""
        payload: dict[str, Any] = {
            "name": type(self).__name__,
            "fail_message": self.fail_message,
            "state": "Fail",
        }
        if self.expected is not MISSING:
            payload["expected"] = _json_safe(self.expected)
        if self.received is not MISSING:
            payload["received"] = _json_safe(self.received)
        return payload


class ContractViolation(TestFailure):
    """A developer bug: suite code did something the verdict contract forbids.

    Raised by the framework, never by suite code. It is a `TestFailure` subclass on
    purpose — the node lands in Fail — but it carries a banner so nobody mistakes a
    broken test for a broken unit.
    """

    BANNER: ClassVar[str] = "CONTRACT VIOLATION"
    DEFAULT_DETAIL: ClassVar[str] = (
        "the node reached a state the author did not explicitly handle; "
        "return PASS or raise TestFailure to resolve"
    )

    def __init__(
        self,
        node: str,
        detail: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Args:
        node: Name of the node whose contract was violated.
        detail: What specifically went wrong. Defaults to the general form.
        cause: The original exception, preserved verbatim for the artifact.
        """
        resolved = detail if detail is not None else self.DEFAULT_DETAIL
        super().__init__(f"{self.BANNER} IN {node}: {resolved}")
        self.node = node
        self.detail = resolved
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause

    @property
    def is_bug(self) -> bool:
        return True

    def to_json(self) -> dict[str, Any]:
        payload = super().to_json()
        payload["banner"] = self.BANNER
        payload["node"] = self.node
        payload["bug"] = True
        if self.cause is not None:
            payload["cause"] = _json_safe(self.cause)
        return payload


@final
class FrameworkContractViolation(ContractViolation):
    """A framework bug — the second layer of the guard, and unreachable by design.

    If this ever appears in a log, litmus itself is wrong. Its banner is distinct from
    the developer layer so the FT knows to escalate to the maintainers rather than to
    the suite author.
    """

    BANNER: ClassVar[str] = "FRAMEWORK CONTRACT VIOLATION"
    DEFAULT_DETAIL: ClassVar[str] = (
        "this should never happen; report it to the framework maintainers with the "
        "full log and reproduction steps"
    )


@final
class MissingVerdict(LitmusError):
    """No verdict was produced: the node returned something other than the sentinel.

    A `None` fall-through or an explicit wrong value. Still a developer contract
    violation, logged as loudly as one — but **not** a failure, and deliberately not a
    `TestFailure`. The node rendered no verdict, and the framework will not forge Fail
    or Pass on its behalf. Renders node-local Aborted; siblings are unaffected, because
    one author's missing `return` must not take down a seventy-DUT run.
    """

    BANNER: ClassVar[str] = "CONTRACT VIOLATION"

    def __init__(self, node: str, returned: object) -> None:
        """Args:
        node: Name of the node that returned without a verdict.
        returned: The value it returned instead of PASS.
        """
        fail_message = (
            f"{self.BANNER} IN {node}: returned {returned!r} instead of the PASS "
            "sentinel; no verdict was produced, so none is reported"
        )
        super().__init__(fail_message)
        self.fail_message = fail_message
        self.node = node
        self.returned = returned

    @property
    def is_bug(self) -> bool:
        """True: the absence of a verdict is always broken code, never broken hardware."""
        return True

    def to_json(self) -> dict[str, Any]:
        """JSON-safe snapshot for the run artifact. Never raises."""
        return {
            "name": type(self).__name__,
            "fail_message": self.fail_message,
            "state": "Aborted",
            "banner": self.BANNER,
            "node": self.node,
            "returned": _json_safe(self.returned),
            "bug": True,
        }


type HookName = Literal["before_each", "after_each"]


@final
class ExternalIntegrationFailure(LitmusError):
    """A harness-level halt: an integration hook threw.

    One instance plays three structurally identical roles — the failure recorded on the
    Aborted node, the reason carried by the run-level abort, and the exception raised
    at the boundary that noticed. One class, three roles, zero wrapper layers. Do not
    introduce a wrapper: this type is never rewrapped by an ancestor, because the
    integrator must see their own error, not the framework's opinion of it.

    The original thrown value survives verbatim on `cause` so a DB driver error or a
    network rejection crosses the framework boundary unrebranded.
    """

    def __init__(self, hook: HookName, cause: BaseException) -> None:
        """Args:
        hook: Which hook threw — `before_each` or `after_each`.
        cause: The integrator's original exception, preserved verbatim.
        """
        fail_message = f"external integration hook {hook}() threw — test run halted: {cause}"
        super().__init__(fail_message)
        self.fail_message = fail_message
        self.hook: HookName = hook
        self.cause = cause
        self.__cause__ = cause

    @property
    def is_bug(self) -> bool:
        """False: the integrator's system failed, not the suite and not the framework."""
        return False

    def to_json(self) -> dict[str, Any]:
        """JSON-safe snapshot for the run artifact. Never raises."""
        return {
            "name": type(self).__name__,
            "fail_message": self.fail_message,
            "state": "Aborted",
            "hook": self.hook,
            "cause": _json_safe(self.cause),
        }


type Failure = TestFailure | ExternalIntegrationFailure | MissingVerdict
"""Everything that can be recorded on a settled node. Pass records nothing."""
