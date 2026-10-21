"""The failure taxonomy.

Three peer terminal states — Pass, Fail, Aborted — and the error types that carry the
latter two. The distinction between them is load-bearing, not cosmetic:

  - `TestFailure` is **data**. It is a real verdict of Fail about the hardware. The
    child boundary catches it and records it; a failing DUT never becomes control flow.
  - `ExternalIntegrationFailure` is **control flow**. It is a harness-level halt raised
    when an integration hook throws. It propagates class-intact through every ancestor,
    is never rewrapped, and cancels the run.
  - `OperatorInterrupt` is **control flow**. It is a harness-level halt raised when the
    operator sends SIGINT or SIGTERM to a live run. The Floor Technician reads the
    operator's name on every Aborted node.
  - `MissingVerdict` is **the absence of data**. The node returned something other than
    the PASS sentinel, so no verdict exists. The framework refuses to forge one; the
    node renders Aborted and its siblings are untouched.

`ContractViolation` and `FrameworkContractViolation` are the two layers of the bug
guard: a developer bug and a framework bug, each with its own banner so the reader of
a log can tell instantly whose code is wrong.
"""

import functools
import signal
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, ClassVar, Literal, cast, final

from litmus.redaction import Redactions


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
    if isinstance(
        value, TestFailure | ExternalIntegrationFailure | MissingVerdict | OperatorInterrupt
    ):
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

    A failure recorded at the child boundary is bound to the run's redactions
    (issue #154): from then on ``str()``, ``repr()``, and ``to_json()`` mask every
    secret the suite declared through ``redact_ctx``, so a ``TestFailure`` that was
    handed a session credential as its ``received`` value renders it as
    ``<REDACTED>`` on every surface. Every ``to_json`` in the taxonomy is wrapped at
    class creation, so a subclass need not know the masking exists.
    """

    _redactions: Redactions | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        to_json = cls.__dict__.get("to_json")
        if callable(to_json):
            cls.to_json = _masking_to_json(cast("Callable[[Any], dict[str, Any]]", to_json))

    def __str__(self) -> str:
        return self._masked(super().__str__())

    def __repr__(self) -> str:
        return self._masked(super().__repr__())

    def _masked(self, text: str) -> str:
        redactions = self._redactions
        return text if redactions is None else redactions.mask(text)

    def _masked_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        redactions = self._redactions
        if redactions is None:
            return payload
        return cast("dict[str, Any]", redactions.mask_json(payload))


def _masking_to_json(
    to_json: Callable[[Any], dict[str, Any]],
) -> Callable[[Any], dict[str, Any]]:
    @functools.wraps(to_json)
    def masked(self: LitmusError) -> dict[str, Any]:
        return self._masked_json(to_json(self))  # pyright: ignore[reportPrivateUsage]

    return masked


def bind_redactions(error: BaseException | None, redactions: Redactions) -> None:
    """Bind ``redactions`` to ``error`` and every litmus error chained beneath it.

    Walks ``cause`` (the taxonomy's own link), ``__cause__``, and ``__context__``, so a
    ``ContractViolation`` raised from an author's ``TestFailure`` masks both. An
    exception outside the taxonomy is left as it is: the framework does not rewrite
    what it did not raise and cannot render.
    """
    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, LitmusError):
            current._redactions = redactions  # pyright: ignore[reportPrivateUsage]
        nested = getattr(current, "cause", None)
        if isinstance(nested, BaseException):
            current = nested
            continue
        current = current.__cause__ or current.__context__


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
        *,
        evidence: Mapping[str, object] | None = None,
    ) -> None:
        """Record what failed and, when given, the comparison that proves it.

        Args:
            fail_message: What failed, in the FT's language.
            expected: The spec-defined value, when there is one.
            received: The value actually observed — unknown, which is why we test.
            evidence: Readings taken before the failure, carried into the leaf's
                ``run_data`` beside the verdict. A failure is not held to the method's
                ``@evidence`` declaration; it records whatever it is handed.
        """
        super().__init__(fail_message)
        self.fail_message = fail_message
        self.expected = expected
        self.received = received
        self.evidence: Mapping[str, object] | None = (
            None if evidence is None else MappingProxyType(dict(evidence))
        )

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
        if self.evidence is not None:
            payload["evidence"] = _json_safe(dict(self.evidence))
        return payload


class ArpResolutionFailure(LitmusError):
    """The resolver could not map a MAC address to an IP.

    This is not a `TestFailure`. That is deliberate:

    - Uncaught in a test method or `configure`, the child boundary classifies it
      as a developer bug. The suite did not model this failure mode at all.
    - Caught and re-raised as `TestFailure`, the author acknowledged the mode
      and chose the verdict shape: the device did not appear on the network.

    Catching this does not tell you *why* the ARP entry is missing — dead NIC,
    unplugged cable, hungry switch, DHCP failure, or a gateway that has not
    seen traffic from the target yet. No amount of code can distinguish those.
    Hardware versus infrastructure is the human triager's call from logs,
    survey artifacts, and the bench. Do not write code that pretends to know.

    If this extended `TestFailure`, an uncaught miss would land in Fail and
    look like a modeled hardware verdict, hiding that the author never handled
    the mode.
    """

    def __init__(self, message: str, *, mac: str, host: str) -> None:
        """Record the MAC that could not be resolved and the host that was asked.

        Args:
            message: What failed, in the FT's language.
            mac: The normalised MAC that was missing from the table.
            host: Routing label of the host whose ARP table was read
                (`"local"`, `"via 10.0.0.1"`, …).
        """
        super().__init__(message)
        self.mac = mac
        self.host = host


class DeviceApiOperationFailure(LitmusError):
    """A device-API transfer did not complete. This is not a DUT verdict.

    Uncaught in a test method or ``configure``, the child boundary classifies it
    as a developer bug: the suite did not model this failure mode. Caught and
    re-raised as ``TestFailure``, the author chose the verdict. A well-formed
    HTTP 4xx/5xx is *not* this error — that status is data on ``DeviceResponse``.
    """

    def __init__(
        self,
        message: str,
        *,
        handle: str,
        operation: str,
        method: str,
        path: str,
        ceiling: float | None = None,
    ) -> None:
        """Record the handle, operation, and ceiling that did not complete.

        Args:
            message: What failed, in the FT's language.
            handle: Scheme plus base URL of the session.
            operation: ``request``, ``connect``, or ``close``.
            method: HTTP/CoAP method, or empty when the operation has none.
            path: Resource path, or empty when the operation has none.
            ceiling: Backend-declared seconds bound, when one expired.
        """
        super().__init__(message)
        self.handle = handle
        self.operation = operation
        self.method = method
        self.path = path
        self.ceiling = ceiling


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
        """Banner the violation with the node it happened in.

        Args:
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

    @classmethod
    def escaped(cls, node: str, error: BaseException) -> ContractViolation:
        """Banner an exception that is not a verdict and escaped suite code.

        The author neither returned PASS nor raised `TestFailure`, so the default
        "return PASS or raise TestFailure" advice would misdescribe what happened.
        The detail names the exception type and message — the thing the reader
        has to fix — and the original stays on `cause` for the artifact.
        """
        message = str(error) or "no message"
        return cls(
            node,
            f"{type(error).__name__} escaped the node: {message}; "
            "a developer bug — catch it and raise TestFailure, or fix the call",
            cause=error,
        )

    @property
    def is_bug(self) -> bool:
        """A contract violation is always a developer bug, never a device fault."""
        return True

    def to_json(self) -> dict[str, Any]:
        """Carry banner, node, and cause so the record names who is at fault and where."""
        payload = super().to_json()
        payload["banner"] = self.BANNER
        payload["node"] = self.node
        payload["bug"] = True
        if self.cause is not None:
            payload["cause"] = _json_safe(self.cause)
        return payload


_FRAMEWORK_ESCALATION = (
    "report it to the framework maintainers with the full log and reproduction steps"
)


@final
class FrameworkContractViolation(ContractViolation):
    """A framework bug — the second layer of the guard, and unreachable by design.

    If this ever appears in a log, litmus itself is wrong. Its banner is distinct from
    the developer layer so the FT knows to escalate to the maintainers rather than to
    the suite author.
    """

    BANNER: ClassVar[str] = "FRAMEWORK CONTRACT VIOLATION"
    DEFAULT_DETAIL: ClassVar[str] = f"this should never happen; {_FRAMEWORK_ESCALATION}"

    def __init__(
        self,
        node: str,
        detail: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Banner a framework bug and name the maintainer escalation.

        Args:
            node: Name of the node whose contract was violated.
            detail: What specifically went wrong. A rule-only detail is completed
                with the maintainer escalation so the author-facing string names
                the fix. Defaults to the general form.
            cause: The original exception, preserved verbatim for the artifact.
        """
        if detail is None:
            resolved = self.DEFAULT_DETAIL
        elif _FRAMEWORK_ESCALATION in detail:
            resolved = detail
        else:
            resolved = f"{detail}; {_FRAMEWORK_ESCALATION}"
        super().__init__(node, resolved, cause)


@final
class ContextInsufficient(ContractViolation):
    """A ``[]`` read of ``utils.ctx`` asked for a key ``configure()`` never provided.

    The contract is that ``configure()`` returns every key a test method, hook, or
    ``cleanup()`` reads by subscript. A missing key is therefore the author's bug, not
    the unit's failure: the node lands in Fail under the contract-violation banner and
    the message names the key, so the fix is one line in ``configure()``. Reads through
    ``.get()`` and ``in`` are the author declaring a key optional and never raise this.

    Raised by the framework's context object, never by suite code. The child boundary
    re-attributes it to the node whose body performed the read, so the banner names the
    test the Floor Technician sees in the tree rather than the instance that owns the
    context. It is deliberately not a ``KeyError``: a suite's ``except KeyError`` must
    not be able to swallow a contract violation (issue #112).
    """

    def __init__(self, node: str, *, key: str, owner: str | None = None) -> None:
        """Name the missing key and, when it differs from ``node``, the context's owner.

        Args:
            node: Name of the node whose body performed the read.
            key: The ctx key that was read but never provided.
            owner: Name of the authored instance whose ``configure()`` owed the key,
                when known.
        """
        detail = f"context insufficient: configure() did not provide {key!r}"
        if owner is not None and owner != node:
            detail = f"{detail} (context owned by {owner!r})"
        detail = f"{detail}; return {key!r} from configure()"
        super().__init__(node, detail)
        self.key = key
        self.owner = owner

    def attributed_to(self, node: str) -> ContextInsufficient:
        """Return the same violation bannered with the node that performed the read."""
        return ContextInsufficient(node, key=self.key, owner=self.owner)

    def to_json(self) -> dict[str, Any]:
        """Carry the missing key and its owner so the record names what ``configure()`` owed."""
        payload = super().to_json()
        payload["key"] = self.key
        if self.owner is not None:
            payload["owner"] = self.owner
        return payload


@final
class ContextImmutable(ContractViolation):
    """A write to ``utils.ctx`` after ``configure()`` returned.

    Ctx is what ``configure()`` provides and nothing more: it is read-only for the
    rest of the lifecycle. A reading belongs on the verdict, ``PASS({...})`` or
    ``TestFailure(..., evidence=...)``; a value cleanup must restore is captured in
    ``configure()``. Raised by the framework's context object, never by suite code, and
    re-attributed by the child boundary to the node whose body performed the write
    (issue #118).
    """

    def __init__(self, node: str, *, key: str, owner: str | None = None) -> None:
        """Name the key that was written and, when it differs from ``node``, the owner.

        Args:
            node: Name of the node whose body performed the write.
            key: The ctx key that was written, or a description of the operation
                when no single key names it.
            owner: Name of the authored instance whose ``configure()`` owns the
                context, when known.
        """
        detail = f"ctx is read-only after configure(): cannot write {key!r}"
        if owner is not None and owner != node:
            detail = f"{detail} (context owned by {owner!r})"
        detail = f"{detail}; return it from configure(), or carry it as evidence on PASS"
        super().__init__(node, detail)
        self.key = key
        self.owner = owner

    def attributed_to(self, node: str) -> ContextImmutable:
        """Return the same violation bannered with the node that performed the write."""
        return ContextImmutable(node, key=self.key, owner=self.owner)

    def to_json(self) -> dict[str, Any]:
        """Carry the written key and its owner so the record names the frozen context touched."""
        payload = super().to_json()
        payload["key"] = self.key
        if self.owner is not None:
            payload["owner"] = self.owner
        return payload


@final
class EvidenceViolation(ContractViolation):
    """The evidence a verdict carried broke the evidence contract.

    Three reasons, one class, so the Floor Technician reads one banner and the message
    names the key: the value cannot be carried as JSON, the key shadows a ctx entry or
    the reserved ``measurements`` slot, or ``@evidence`` declared the key and ``PASS``
    did not carry it. A pass without its declared evidence is not a pass: the node
    lands in Fail under the contract-violation banner and no hardware verdict is
    forged (issue #118).
    """

    def __init__(self, node: str, *, key: str, reason: str) -> None:
        """Name the offending key and why it offends.

        Args:
            node: Name of the leaf whose verdict carried the evidence.
            key: The evidence key at fault.
            reason: One of the three sentences above, in the FT's language.
        """
        super().__init__(node, f"{reason} {key!r}")
        self.key = key
        self.reason = reason

    @classmethod
    def not_carried(cls, node: str, key: str) -> EvidenceViolation:
        """``@evidence`` declared ``key`` and ``PASS`` did not carry it."""
        return cls(
            node,
            key=key,
            reason="evidence incomplete: PASS did not carry declared key",
        )

    @classmethod
    def not_serializable(cls, node: str, key: str) -> EvidenceViolation:
        """The value under ``key`` cannot be carried as JSON."""
        return cls(
            node,
            key=key,
            reason="evidence is not JSON-serializable at key",
        )

    @classmethod
    def shadows_context(cls, node: str, key: str) -> EvidenceViolation:
        """``key`` is already a ctx entry or the reserved measurements slot."""
        return cls(
            node,
            key=key,
            reason="evidence shadows a ctx key or reserved slot:",
        )

    def to_json(self) -> dict[str, Any]:
        """Carry the offending key so the record names the collision, not just the class."""
        payload = super().to_json()
        payload["key"] = self.key
        return payload


class MeasurementViolation(ContractViolation):
    """A ``utils.measure`` call the measurement protocol refuses (issue #86).

    The four guards below are tier 1 on purpose: a measurement is a record the process
    engineer reads without the suite source, so a malformed, duplicated, or misplaced
    reading is a developer bug the framework refuses at the call rather than a row a
    linter might have caught. Each names the node and the measurement so the Floor
    Technician's banner and the developer's traceback agree on where the suite is wrong.
    Private by ruling: ``measure`` and ``Measurement`` are the two public names.
    """

    def __init__(self, node: str, *, measurement: str, detail: str) -> None:
        """Banner the violation with the node and the measurement name.

        Args:
            node: Name of the node whose body made the call.
            measurement: The ``name`` argument of the refused call, as given.
            detail: What specifically was refused.
        """
        super().__init__(node, detail)
        self.measurement = measurement

    def to_json(self) -> dict[str, Any]:
        """Carry the measurement name so the record ties the refusal to its reading."""
        payload = super().to_json()
        payload["measurement"] = self.measurement
        return payload


@final
class MeasurementInvalid(MeasurementViolation):
    """An argument to ``utils.measure`` that the protocol does not accept.

    A name that is empty, a value that is ``bool``, ``NaN``, or infinite, a unit that is
    not text, or a window that bounds nothing: each would corrupt the series a consumer
    keys on this name, so the reading is refused instead of recorded.
    """

    def __init__(self, node: str, *, measurement: str, reason: str) -> None:
        """Name the measurement and state the rule it broke.

        Args:
            node: Name of the node whose body made the call.
            measurement: The ``name`` argument as given (``repr`` when not text).
            reason: The rule the argument broke, in the developer's terms.
        """
        super().__init__(
            node,
            measurement=measurement,
            detail=f"measurement {measurement!r} refused: {reason}",
        )
        self.reason = reason

    def to_json(self) -> dict[str, Any]:
        """Carry the reason so the record says why the value was refused, not only that it was."""
        payload = super().to_json()
        payload["reason"] = self.reason
        return payload


@final
class MeasurementDuplicate(MeasurementViolation):
    """The same measurement name recorded twice in one node.

    A consumer that sees two ``output_voltage_v`` rows for one node cannot tell a retry
    from a bug, so the second reading is refused rather than appended.
    """

    def __init__(self, node: str, *, measurement: str) -> None:
        """Name the node and the measurement recorded twice."""
        super().__init__(
            node,
            measurement=measurement,
            detail=(
                f"measurement {measurement!r} was already recorded in this node; "
                "a name identifies one quantity per node, so take the reading once "
                "or give the second reading its own name"
            ),
        )


@final
class MeasurementSealed(MeasurementViolation):
    """A ``utils.measure`` call outside ``configure()`` or the test method.

    Those two slots produce the node's record; ``cleanup``, surveys, audits, and
    integration hooks do not, and a node that has settled has nothing left to record
    into. The record is sealed at the same moment ``run_data`` is snapshotted, in the
    pattern of ``ContextImmutable``.
    """

    def __init__(self, node: str, *, measurement: str) -> None:
        """Name the node and the measurement that arrived outside its slots."""
        super().__init__(
            node,
            measurement=measurement,
            detail=(
                f"measurement {measurement!r} recorded outside configure() or the test "
                "method; the record is sealed like ctx, so cleanup, surveys, audits, "
                "and hooks cannot measure"
            ),
        )


@final
class MeasurementUnbound(MeasurementViolation):
    """``litmus.measure`` called where no node's slot is executing.

    The package-root alias resolves the current node through a task-local binding the
    framework sets around ``configure()`` and the test method. At module scope, in a
    hook, in ``cleanup``, or in a helper called from outside a run there is no node to
    record against, so the call is refused rather than recorded somewhere surprising.
    """

    def __init__(self, measurement: str) -> None:
        """Name the measurement; the banner names the alias, since there is no node."""
        super().__init__(
            "litmus.measure",
            measurement=measurement,
            detail=(
                f"measure({measurement!r}) was called outside a running node; call "
                "utils.measure inside configure() or a test method"
            ),
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
        """Record the non-verdict a node returned.

        Args:
            node: Name of the node that returned without a verdict.
            returned: The value it returned instead of PASS.
        """
        fail_message = (
            f"{self.BANNER} IN {node}: returned {returned!r} instead of the PASS "
            "sentinel; return PASS or raise TestFailure"
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
        """Carry the integrator's error across the framework without rebranding it.

        Args:
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


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


@final
class OperatorInterrupt(LitmusError):
    """A harness-level halt: the operator interrupted the run.

    One instance is the failure recorded on every Aborted node and the reason
    carried by the run-level abort. It is not a `TestFailure` and not an
    integration-hook failure: the recovery is "the operator stopped the rack."
    """

    def __init__(self, signum: int) -> None:
        """Name the operator and the signal that stopped the run.

        Args:
            signum: The POSIX signal number (`SIGINT`, `SIGTERM`).
        """
        name = _signal_name(signum)
        fail_message = f"operator interrupted the test run ({name})"
        super().__init__(fail_message)
        self.fail_message = fail_message
        self.signum = int(signum)
        self.signal_name = name

    @property
    def is_bug(self) -> bool:
        """False: the operator stopped the run, not the suite and not the hardware."""
        return False

    def to_json(self) -> dict[str, Any]:
        """JSON-safe snapshot for the run artifact. Never raises."""
        return {
            "name": type(self).__name__,
            "fail_message": self.fail_message,
            "state": "Aborted",
            "operator": True,
            "signal": self.signal_name,
        }


type HaltReason = ExternalIntegrationFailure | OperatorInterrupt
"""Everything that can fire the run-level abort. First reason wins."""


type Failure = TestFailure | HaltReason | MissingVerdict
"""Everything that can be recorded on a settled node. Pass records nothing."""
