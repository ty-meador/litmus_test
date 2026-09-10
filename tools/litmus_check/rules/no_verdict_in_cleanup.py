"""``litmus/no-verdict-in-cleanup`` — ``cleanup`` restores state; it does not verdict.

``cleanup`` always runs, and its return is discarded. Errors thrown from it are
logged and swallowed so a teardown bug cannot forge or mask a hardware verdict.
``return PASS`` or ``raise TestFailure`` in that method is a copy-paste from a test
method that silently does nothing the Floor Technician can act on.

Two findings, two messages:

- ``return PASS`` (bare or module-qualified) — pass logic in the wrong phase.
- ``raise TestFailure(...)`` — a Fail assertion the framework will not propagate.

Not flagged: a plain ``ValueError`` / ``RuntimeError`` in ``cleanup`` (logged, and
not this rule's idiom), ``return PASS`` in a test method, or a ``cleanup`` on a
class that is not a ``LitmusTest``. Nested functions inherit the enclosing method:
a helper defined inside ``cleanup`` is still in ``cleanup``.

Tier-1 counterpart: cleanup's return is discarded and its exceptions are swallowed.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from litmus_check.model import Rule, SuiteModule, Violation

_CLEANUP = "cleanup"
_PASS = "PASS"
_TEST_FAILURE = "TestFailure"


class NoVerdictInCleanupRule(Rule):
    """Flag pass/fail intent leaking into ``cleanup``."""

    code = "litmus/no-verdict-in-cleanup"
    summary = "cleanup restores state and does not produce a verdict"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        """Report pass/fail intent leaking into ``cleanup``.

        See the ``litmus/no-verdict-in-cleanup`` row in ``docs/litmus-check.md``.
        """
        for klass in module.litmus_classes():
            for member in klass.body:
                is_def = isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef)
                if is_def and member.name == _CLEANUP:
                    yield from self._check_cleanup(module, member)

    def _check_cleanup(
        self, module: SuiteModule, cleanup: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> Iterable[Violation]:
        for node in ast.walk(cleanup):
            if isinstance(node, ast.Return) and _returns_pass(node):
                yield module.violation(
                    node,
                    self.code,
                    "cleanup does not produce a verdict — its return is discarded. "
                    "Move pass logic to a test method; cleanup restores hardware state",
                )
                continue
            if isinstance(node, ast.Raise) and _raised_constructor_name(node) == _TEST_FAILURE:
                yield module.violation(
                    node,
                    self.code,
                    "cleanup failures are swallowed (logged, not propagated). "
                    "Raising TestFailure here will not fail the test; move the "
                    "assertion to a test method",
                )


def _returns_pass(node: ast.Return) -> bool:
    value = node.value
    if isinstance(value, ast.Call):
        # ``return PASS({...})`` carries evidence; it is still a PASS (issue #118).
        value = value.func
    if isinstance(value, ast.Name):
        return value.id == _PASS
    return isinstance(value, ast.Attribute) and value.attr == _PASS


def _raised_constructor_name(node: ast.Raise) -> str | None:
    exc = node.exc
    if exc is None:
        return None
    target: ast.expr = exc.func if isinstance(exc, ast.Call) else exc
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None
