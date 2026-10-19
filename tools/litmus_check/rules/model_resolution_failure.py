"""``litmus/model-resolution-failure`` — a resolver or device-API miss is a verdict, not a bug.

``ArpResolver.resolve`` / ``reacquire`` raise ``ArpResolutionFailure`` and a device
API's ``request`` raises ``DeviceApiOperationFailure``. Neither is a ``TestFailure``.
Left uncaught, the failure taxonomy classifies it as a developer-bug Fail ("THIS IS A
BUG IN <name>"), and the Floor Technician reads a software defect where the bench
had a device that never appeared on the network. The suite did not model the miss.

Flagged: an awaited ``.resolve()``, ``.reacquire(...)``, or ``.request(...)`` call
inside a statically known ``LitmusTest`` subclass that does not sit in the body of a
``try`` with at least one handler that raises ``TestFailure``.

In ``cleanup`` the guard is the opposite handler. Cleanup gives no verdict and must not
throw (Lifecycle Contract), and ``litmus/no-verdict-in-cleanup`` forbids the
``TestFailure`` this rule asks for everywhere else, so the two rules once had no shape
that satisfied both (issue #153). There the call is guarded by a ``try`` with at least
one handler that raises nothing: the idiom logs the miss, and the rest of the teardown still
runs instead of being skipped by an exception the framework only swallows. Nested
functions inherit the enclosing method, as in ``litmus/no-verdict-in-cleanup``.

Not flagged: the same call inside such a ``try``, wherever the handler's raise sits
in its body; the call outside a ``LitmusTest`` class; or the call not awaited (a
helper that returns the coroutine for a caller to guard).

Tier-1 counterpart: the failure taxonomy of issue #3 classifies the escaping exception
as a developer bug. That is correct and unhelpful; this rule moves the fix to the
editor, where the message names the exception to catch. In ``cleanup`` the escaping
exception is logged and swallowed, which ends the teardown early.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable

from litmus_check.model import Rule, SuiteModule, Violation

_RESOLVING: dict[str, str] = {
    "resolve": "ArpResolutionFailure",
    "reacquire": "ArpResolutionFailure",
    "request": "DeviceApiOperationFailure",
}
_VERDICT = "TestFailure"
_CLEANUP = "cleanup"


class ModelResolutionFailureRule(Rule):
    """Flag an awaited resolve/reacquire/request outside the ``try`` its method needs."""

    code = "litmus/model-resolution-failure"
    summary = "catch ArpResolutionFailure / DeviceApiOperationFailure and raise TestFailure"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        """Report every unguarded resolution call inside a suite class.

        See the ``litmus/model-resolution-failure`` row in ``docs/litmus-check.md``.
        """
        for klass in module.litmus_classes():
            in_cleanup = _awaits_in_cleanup(klass)
            for node in ast.walk(klass):
                if not isinstance(node, ast.Await):
                    continue
                spelling = _resolution_call(node.value)
                if spelling is None:
                    continue
                raised = _RESOLVING[spelling]
                if node in in_cleanup:
                    if not _guarded(module, node, _raises_nothing):
                        yield module.violation(
                            node,
                            self.code,
                            f"{spelling}() can raise {raised}, and cleanup must not throw: "
                            "an escaping exception is swallowed and skips the rest of the "
                            "teardown. Catch it and log it through utils.logger; cleanup "
                            "gives no verdict, so raise nothing",
                        )
                    continue
                if not _guarded(module, node, _raises_verdict):
                    yield module.violation(
                        node,
                        self.code,
                        f"{spelling}() can raise {raised}, which is not a TestFailure; uncaught "
                        "it is classified as a developer bug. Catch it and raise TestFailure so "
                        "the miss is a verdict the Floor Technician can act on",
                    )


def _awaits_in_cleanup(klass: ast.ClassDef) -> set[ast.Await]:
    """Every await under the class's own ``cleanup``, nested helpers included."""
    return {
        node
        for member in klass.body
        if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef) and member.name == _CLEANUP
        for node in ast.walk(member)
        if isinstance(node, ast.Await)
    }


def _resolution_call(node: ast.expr) -> str | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    attr = node.func.attr
    return attr if attr in _RESOLVING else None


def _guarded(
    module: SuiteModule, awaited: ast.Await, handles: Callable[[ast.ExceptHandler], bool]
) -> bool:
    """True when the await sits in the body of a ``try`` with a handler that ``handles`` it."""
    child: ast.AST = awaited
    for ancestor in module.ancestors(awaited):
        if isinstance(ancestor, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            return False
        if (
            isinstance(ancestor, ast.Try | ast.TryStar)
            and _in_body(ancestor, child)
            and any(handles(handler) for handler in ancestor.handlers)
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


def _raises_nothing(handler: ast.ExceptHandler) -> bool:
    # Any raise counts against the handler: a TestFailure is the verdict cleanup cannot
    # give, and a bare or different raise is the throw the Lifecycle Contract forbids.
    return not any(isinstance(node, ast.Raise) for node in ast.walk(handler))
