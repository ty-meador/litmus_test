"""``litmus/measurement-name-unique`` — one name records one reading per node.

A measurement's series is ``(station, node path, name)``: the consumer aggregating
``result.json`` across runs keys on the name, so a second ``measure("rail_v", ...)`` in
the same node would either overwrite the first reading or fork the series. The
framework refuses the second call at runtime; this rule refuses it in the editor, where
the developer can still see both calls side by side.

Flagged: two ``measure(...)`` calls with the same string-literal name inside one
method of a statically known ``LitmusTest`` subclass, either spelling (``utils.measure``
or the package-root ``measure``; the receiver is not checked by name, ``utils`` is the
convention, not the rule). The second and every later call is reported. A closure
defined inside the method counts as the method: it records on the same node.

Not flagged: a computed name, a name repeated across two methods (two nodes), or one
call inside a loop. The rule reads only what is literal; tier 3 is skippable by design
and tier 1 is the guard.

Tier-1 counterpart: the duplicate-name guard in ``TestUtils.measure`` and
``litmus.measure`` raises ``ContractViolation`` naming the node and the measurement.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator

from litmus_check.model import Rule, SuiteModule, Violation

_MEASURE = "measure"
_NAME = "name"


class MeasurementNameUniqueRule(Rule):
    """Flag a literal measurement name recorded twice in one method."""

    code = "litmus/measurement-name-unique"
    summary = "a measurement name is recorded once per node"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        """Walk each method of each suite class on its own; a name is unique per node."""
        for klass in module.litmus_classes():
            for member in klass.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    yield from self._check_method(module, member)

    def _check_method(
        self, module: SuiteModule, method: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> Iterator[Violation]:
        seen: set[str] = set()
        for call in _measure_calls(method):
            name = measurement_name(call)
            if name is None:
                continue
            if name in seen:
                yield module.violation(
                    call,
                    self.code,
                    f"measurement {name!r} is recorded twice in {method.name}: a node records "
                    "one reading per name, and tier 1 refuses the second call; give the "
                    "second reading its own name",
                )
            seen.add(name)


def _measure_calls(method: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.Call]:
    """Every ``measure(...)`` call in the method body, in source order.

    ``ast.walk`` is breadth-first, which would report a closure's call before the
    method-body call that follows it in the file; the second call in the source is the
    one the developer expects to see flagged.
    """
    calls = [node for node in ast.walk(method) if isinstance(node, ast.Call) and is_measure(node)]
    yield from sorted(calls, key=lambda call: (call.lineno, call.col_offset))


def is_measure(call: ast.Call) -> bool:
    """True for ``<receiver>.measure(...)`` and the package-root ``measure(...)``."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr == _MEASURE
    return isinstance(func, ast.Name) and func.id == _MEASURE


def measurement_name(call: ast.Call) -> str | None:
    """The literal name a ``measure`` call records under, or ``None`` when computed."""
    argument: ast.expr | None = call.args[0] if call.args else None
    for keyword in call.keywords:
        if keyword.arg == _NAME:
            argument = keyword.value
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    return None
