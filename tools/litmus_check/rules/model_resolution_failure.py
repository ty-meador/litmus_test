"""``litmus/model-resolution-failure`` — a resolver or device-API miss is a verdict, not a bug.

``ArpResolver.resolve`` / ``reacquire`` raise ``ArpResolutionFailure`` and a device
API's ``request`` raises ``DeviceApiOperationFailure``. Neither is a ``TestFailure``.
Left uncaught, the failure taxonomy classifies it as a developer-bug Fail ("THIS IS A
BUG IN <name>"), and the Floor Technician reads a software defect where the bench
had a device that never appeared on the network. The suite did not model the miss.

Flagged: an awaited ``.resolve()``, ``.reacquire(...)``, or ``.request(...)`` call
inside a statically known ``LitmusTest`` subclass that does not sit in the body of a
``try`` with at least one handler that raises ``TestFailure``.

Not flagged: the same call inside such a ``try``, wherever the handler's raise sits
in its body; the call outside a ``LitmusTest`` class; or the call not awaited (a
helper that returns the coroutine for a caller to guard).

Tier-1 counterpart: the failure taxonomy of issue #3 classifies the escaping exception
as a developer bug. That is correct and unhelpful; this rule moves the fix to the
editor, where the message names the exception to catch.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from litmus_check.model import Rule, SuiteModule, Violation

_RESOLVING: dict[str, str] = {
    "resolve": "ArpResolutionFailure",
    "reacquire": "ArpResolutionFailure",
    "request": "DeviceApiOperationFailure",
}
_VERDICT = "TestFailure"


class ModelResolutionFailureRule(Rule):
    """Flag an awaited resolve/reacquire/request outside a ``TestFailure``-raising ``try``."""

    code = "litmus/model-resolution-failure"
    summary = "catch ArpResolutionFailure / DeviceApiOperationFailure and raise TestFailure"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        """Report every unguarded resolution call inside a suite class.

        See the ``litmus/model-resolution-failure`` row in ``docs/litmus-check.md``.
        """
        for klass in module.litmus_classes():
            for node in ast.walk(klass):
                if not isinstance(node, ast.Await):
                    continue
                spelling = _resolution_call(node.value)
                if spelling is None or _guarded(module, node):
                    continue
                raised = _RESOLVING[spelling]
                yield module.violation(
                    node,
                    self.code,
                    f"{spelling}() can raise {raised}, which is not a TestFailure; uncaught it "
                    "is classified as a developer bug. Catch it and raise TestFailure so the "
                    "miss is a verdict the Floor Technician can act on",
                )


def _resolution_call(node: ast.expr) -> str | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    attr = node.func.attr
    return attr if attr in _RESOLVING else None


def _guarded(module: SuiteModule, awaited: ast.Await) -> bool:
    """True when the await sits in the body of a ``try`` whose handlers raise ``TestFailure``."""
    child: ast.AST = awaited
    for ancestor in module.ancestors(awaited):
        if isinstance(ancestor, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            return False
        if (
            isinstance(ancestor, ast.Try | ast.TryStar)
            and _in_body(ancestor, child)
            and any(_raises_verdict(handler) for handler in ancestor.handlers)
        ):
            return True
        child = ancestor
    return False


def _in_body(block: ast.Try | ast.TryStar, child: ast.AST) -> bool:
    return any(child is stmt for stmt in block.body)


def _raises_verdict(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(handler):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        func = node.exc.func
        if isinstance(func, ast.Name) and func.id == _VERDICT:
            return True
        if isinstance(func, ast.Attribute) and func.attr == _VERDICT:
            return True
    return False
