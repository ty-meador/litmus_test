"""Parametric measurements: a number with a unit and limits, recorded beside the verdict.

The rule this module serves is stated in ``docs/measurements.md``: a limit is evidence,
never a verdict. ``Measurement`` carries ``in_limits`` as a recorded fact; nothing here
raises on an out-of-window reading or touches a node's state.

Three things live here, kept apart on purpose. The value type and its validation are
pure and know nothing about a run. ``current_utils`` is the task-local binding that
lets the package-root ``measure`` find the node whose slot is executing, without a
global that a ``parallel`` node's children would share. ``measure`` itself is the alias
Ty asked for on issue #85: the same call as ``utils.measure``, resolved through that
binding, and refused when nothing is bound. Recording and publishing belong to
``TestUtils`` and ``base.py`` respectively, so the alias never becomes a second
mechanism.
"""

from __future__ import annotations

import math
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeGuard, cast, final

from litmus.errors import MeasurementInvalid, MeasurementUnbound
from litmus.run_data import JsonObject

if TYPE_CHECKING:
    from litmus.utils import TestUtils

type Limits = tuple[float | None, float | None]
"""An inclusive ``(low, high)`` window; ``None`` on a side leaves that side open."""

current_utils: ContextVar[TestUtils | None] = ContextVar("litmus_current_utils", default=None)
"""The ``TestUtils`` whose ``configure()`` or test method is executing in this task.

Bound by the framework around exactly those two slots (``base.py``), and reset in a
``finally`` when the slot settles. A ``ContextVar`` is task-local and asyncio copies the
context into every task it creates, so each child of a ``parallel`` node sees its own
binding, and an audit sampler or a cleanup, which run outside the slots, see none.
Private to the framework: it is the guard behind ``utils.measure`` as well as the
resolver behind ``litmus.measure``, and nothing else reads it.
"""


@final
@dataclass(frozen=True, slots=True)
class Measurement:
    """One ``utils.measure`` call, exactly as recorded.

    Frozen because the record is sealed with the node: a consumer keys a time series
    on ``name`` together with the node path, so a value that could change after the
    call would corrupt the series it belongs to. ``in_limits`` is ``None`` when the
    reading was recorded without a window, ``True`` or ``False`` otherwise; it is a
    recorded fact, and the verdict is decided elsewhere.

    Attributes:
        name: The identity of the quantity, unique within the node.
        value: The reading, an ``int`` or a finite ``float``.
        unit: The developer's label for the reading, or ``None`` for a dimensionless
            count. Never validated, never converted.
        limits: The inclusive ``(low, high)`` window, or ``None`` when none was given.
        in_limits: Whether ``value`` sits inside ``limits``; ``None`` without limits.
        timestamp: When the reading was recorded, timezone-aware UTC.
    """

    name: str
    value: int | float
    unit: str | None
    limits: Limits | None
    in_limits: bool | None
    timestamp: datetime

    def to_json(self) -> JsonObject:
        """Return the version-1 record shape from ``docs/measurements.md``.

        ``limits`` becomes an object with explicit ``low`` and ``high`` keys rather than
        a bare array so that an open side reads as ``null`` by name, and the timestamp
        takes the timeline's own UTC ``Z`` form so one clock reads across both records.
        """
        limits: JsonObject | None = None
        if self.limits is not None:
            low, high = self.limits
            limits = {"low": low, "high": high}
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "limits": limits,
            "in_limits": self.in_limits,
            "timestamp": self.timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }


def _is_finite_number(value: object) -> TypeGuard[int | float]:
    """Accept ``int`` and finite ``float``; refuse ``bool``, ``NaN``, and infinities.

    ``bool`` is an ``int`` to Python and a category to a process engineer; ``NaN`` and
    the infinities cannot cross a JSON boundary. Both refusals protect the consumer
    who reads the record without the suite source.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def measurement_label(name: object) -> str:
    """Render a measurement name for a violation banner, even when it is not text."""
    return name if isinstance(name, str) else repr(name)


def new_measurement(
    node: str,
    name: object,
    value: object,
    *,
    unit: object,
    limits: object,
) -> Measurement:
    """Validate one reading and return its sealed record.

    Every refusal is a ``MeasurementInvalid`` naming ``node`` and the measurement, so
    the Floor Technician's banner and the developer's traceback agree on where the
    suite is wrong. The arguments arrive untyped on purpose: the public signatures are
    typed for the editor, but the record's integrity is a tier-1 rule, so the checks
    run at the call whatever the caller's annotations said. Validation lives here
    rather than on ``TestUtils`` so the rules are testable without a run and identical
    through either spelling of the call.

    Args:
        node: Name of the node recording the measurement, for the violation banner.
        name: Non-empty identity of the quantity.
        value: An ``int`` or finite ``float``.
        unit: Free label or ``None``.
        limits: ``None``, or an inclusive ``(low, high)`` tuple with at most one open
            side, finite bounds, and ``low <= high``.

    Returns:
        The frozen record, timestamped now.

    Raises:
        MeasurementInvalid: On any argument the protocol refuses.
    """
    if not isinstance(name, str) or not name:
        raise MeasurementInvalid(
            node, measurement=measurement_label(name), reason="name must be a non-empty str"
        )
    if not _is_finite_number(value):
        raise MeasurementInvalid(
            node,
            measurement=name,
            reason=f"value must be an int or a finite float, not {value!r}",
        )
    if unit is not None and not isinstance(unit, str):
        raise MeasurementInvalid(
            node, measurement=name, reason=f"unit must be a str or None, not {unit!r}"
        )
    in_limits: bool | None = None
    window: Limits | None = None
    if limits is not None:
        window = _validated_limits(node, name, limits)
        low, high = window
        in_limits = (low is None or value >= low) and (high is None or value <= high)
    return Measurement(
        name=name,
        value=value,
        unit=unit,
        limits=window,
        in_limits=in_limits,
        timestamp=datetime.now(UTC),
    )


def _validated_limits(node: str, name: str, limits: object) -> Limits:
    """Refuse a window that bounds nothing or is not a window at all.

    Both sides ``None``, a bound that is not a finite number, or ``low > high`` is a
    bug in the suite rather than a wide window, so each is a violation at the call
    instead of a reading recorded against a meaningless limit.
    """
    # The shape check runs on the type object, not on ``limits`` itself, so the
    # element types below stay ``object`` for the finite-number guard to narrow.
    if not issubclass(type(limits), tuple):
        raise MeasurementInvalid(
            node,
            measurement=name,
            reason=f"limits must be a (low, high) tuple or None, not {limits!r}",
        )
    bounds = cast("tuple[object, ...]", limits)
    if len(bounds) != 2:
        raise MeasurementInvalid(
            node,
            measurement=name,
            reason=f"limits must be a (low, high) tuple or None, not {limits!r}",
        )
    sides: list[float | None] = []
    for side, bound in zip(("low", "high"), bounds, strict=True):
        if bound is None:
            sides.append(None)
        elif _is_finite_number(bound):
            sides.append(bound)
        else:
            raise MeasurementInvalid(
                node,
                measurement=name,
                reason=f"limits {side} bound must be a finite number or None, not {bound!r}",
            )
    low, high = sides
    if low is None and high is None:
        raise MeasurementInvalid(
            node, measurement=name, reason="limits=(None, None) bounds nothing; pass limits=None"
        )
    if low is not None and high is not None and low > high:
        raise MeasurementInvalid(
            node, measurement=name, reason=f"limits low {low!r} exceeds high {high!r}"
        )
    return (low, high)


def measure(
    name: str,
    value: int | float,
    *,
    unit: str | None = None,
    limits: Limits | None = None,
) -> Measurement:
    """Record a measurement on the node whose ``configure()`` or test method is running.

    The package-root spelling of ``utils.measure`` (issue #85, Ty's condition): the
    same signature, the same rules, the same record. It exists so ``from litmus import
    measure`` reads naturally in a suite; ``utils.measure`` remains the documented home.
    Outside a running slot there is no node to record against, so the call is refused
    rather than recorded somewhere surprising.

    Args:
        name: Non-empty identity of the quantity, unique within the node.
        value: An ``int`` or finite ``float``.
        unit: Free label such as ``"V"`` or ``"ms"``, or ``None`` for a count.
        limits: Inclusive ``(low, high)`` window, ``None`` on an open side, or ``None``.

    Returns:
        The frozen ``Measurement``, with ``in_limits`` already decided.

    Raises:
        MeasurementUnbound: When no node's ``configure()`` or test method is executing
            in the current task.
    """
    utils = current_utils.get()
    if utils is None:
        raise MeasurementUnbound(name)
    return utils.measure(name, value, unit=unit, limits=limits)
