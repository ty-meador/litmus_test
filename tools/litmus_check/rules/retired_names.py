"""``litmus/retired-names`` — ``case`` and ``group`` are gone; compose with the live vocabulary.

Issue #110 settled the composition vocabulary at ``self.test`` (leaf), ``self.sequence``
(dependent children, in order) and ``self.parallel`` (independent children, concurrent).
``self.case`` and ``self.group`` were removed outright rather than deprecated: the
framework is pre-1.0 and one spelling per idea keeps the suite readable aloud.

Flagged: a call to ``.case(...)`` or ``.group(...)`` on the receiver (``self``) inside a
method of a statically known ``LitmusTest`` subclass.

Not flagged: the same attribute on any other object (``match.group(1)`` is a regular
expression, not a composition), calls outside a ``LitmusTest`` class, or the live names.

Tier-1 counterpart: the attributes no longer exist on ``LitmusTest``; ``build_suite``
raises ``AttributeError`` at composition time, before any hardware is touched. The rule
turns that traceback into a message that names the replacement.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from litmus_check.model import Rule, SuiteModule, Violation

type FunctionDef = ast.FunctionDef | ast.AsyncFunctionDef

_REPLACEMENTS: dict[str, str] = {"case": "test", "group": "parallel"}


class RetiredNamesRule(Rule):
    """Flag ``self.case`` and ``self.group`` inside a ``LitmusTest`` subclass."""

    code = "litmus/retired-names"
    summary = "compose with test(), sequence(), and parallel(); case() and group() are retired"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        for klass in module.litmus_classes():
            for node in ast.walk(klass):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                replacement = _REPLACEMENTS.get(node.func.attr)
                if replacement is None:
                    continue
                method = module.enclosing_method(node)
                if method is None:
                    continue
                receiver = _first_parameter(method)
                if receiver is None or not _is_receiver(node.func.value, receiver):
                    continue
                yield module.violation(
                    node,
                    self.code,
                    f"{node.func.attr}() was retired on issue #110; "
                    f"compose with {replacement}() instead",
                )


def _first_parameter(function: FunctionDef) -> str | None:
    positional = function.args.posonlyargs + function.args.args
    if not positional:
        return None
    return positional[0].arg


def _is_receiver(node: ast.expr, receiver: str) -> bool:
    return isinstance(node, ast.Name) and node.id == receiver
