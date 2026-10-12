"""``litmus/no-swallowed-cancel`` — cancellation is the abort signal; suite code lets it pass.

A run-abort, a deadline, and an operator interrupt all reach a test method the same
way: ``asyncio.CancelledError`` delivered at an ``await``. The child boundary turns
that into ``Aborted`` and the in-flight transport teardown depends on the exception
reaching it. A handler that catches it and does not re-raise converts the abort into
whatever the handler returns or raises: a ``Fail`` the hardware did not earn, or a
``PASS`` minted after the harness gave up.

Flagged: inside an ``async`` method of a statically known ``LitmusTest`` subclass, an
``except`` handler whose type is bare, ``BaseException``, or ``CancelledError`` (under
``asyncio.`` or imported bare), guarding a ``try`` body that contains an ``await``,
when the handler body does not re-raise with a bare ``raise``. Also flagged:
``contextlib.suppress(...)`` naming ``BaseException`` or ``CancelledError``.

Not flagged: ``except Exception`` and narrower (``CancelledError`` is a
``BaseException``, so these let it through), a handler that ends in a bare ``raise``
after its own cleanup, a ``try`` with no ``await`` in its body (nothing there can be
cancelled), or synchronous methods.

Tier-1 counterpart: the cancellation discipline of issue #38 shields evidence
collection and expects suite code to propagate. This rule is the editor-time half.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from litmus_check.model import Rule, SuiteModule, Violation

_SWALLOWING: frozenset[str] = frozenset({"BaseException", "CancelledError"})


class NoSwallowedCancelRule(Rule):
    """Flag handlers that catch ``CancelledError`` inside a ``LitmusTest`` subclass."""

    code = "litmus/no-swallowed-cancel"
    summary = "let asyncio.CancelledError propagate; catch Exception for the failure you modeled"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        """Report every handler that would swallow a cancellation.

        See the ``litmus/no-swallowed-cancel`` row in ``docs/litmus-check.md``.
        """
        for klass in module.litmus_classes():
            for node in ast.walk(klass):
                if isinstance(node, ast.Try | ast.TryStar):
                    yield from self._check_try(module, node)
                elif isinstance(node, ast.Call) and _is_suppress_of_cancel(node):
                    yield from self._check_suppress(module, node)

    def _check_try(self, module: SuiteModule, block: ast.Try | ast.TryStar) -> Iterable[Violation]:
        if not isinstance(module.enclosing_function(block), ast.AsyncFunctionDef):
            return
        if not any(isinstance(n, ast.Await) for stmt in block.body for n in ast.walk(stmt)):
            return
        for handler in block.handlers:
            if not _catches_cancel(handler.type) or _reraises(handler):
                continue
            caught = "a bare except" if handler.type is None else ast.unparse(handler.type)
            yield module.violation(
                handler,
                self.code,
                f"{caught} swallows asyncio.CancelledError, the only signal a run-abort, "
                "a deadline, or an operator interrupt sends; catch Exception for the "
                "failure you modeled, or end the handler with a bare raise",
            )

    def _check_suppress(self, module: SuiteModule, call: ast.Call) -> Iterable[Violation]:
        if not isinstance(module.enclosing_function(call), ast.AsyncFunctionDef):
            return
        yield module.violation(
            call,
            self.code,
            "contextlib.suppress here swallows asyncio.CancelledError, the only signal a "
            "run-abort, a deadline, or an operator interrupt sends; suppress Exception or "
            "narrower for the failure you modeled",
        )


def _catches_cancel(handler_type: ast.expr | None) -> bool:
    if handler_type is None:
        return True
    names = handler_type.elts if isinstance(handler_type, ast.Tuple) else [handler_type]
    return any(_names_cancel(name) for name in names)


def _names_cancel(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id in _SWALLOWING
    return isinstance(node, ast.Attribute) and node.attr in _SWALLOWING


def _reraises(handler: ast.ExceptHandler) -> bool:
    """A handler whose own body ends in a bare ``raise`` hands the cancellation on."""
    return bool(handler.body) and _is_bare_raise(handler.body[-1])


def _is_bare_raise(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Raise) and stmt.exc is None


def _is_suppress_of_cancel(call: ast.Call) -> bool:
    func = call.func
    is_suppress = (isinstance(func, ast.Attribute) and func.attr == "suppress") or (
        isinstance(func, ast.Name) and func.id == "suppress"
    )
    return is_suppress and any(_names_cancel(arg) for arg in call.args)
