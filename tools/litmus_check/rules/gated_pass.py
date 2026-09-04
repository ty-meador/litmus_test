"""``litmus/gated-pass`` — every ``return PASS`` sits inside a conditional branch.

The pass predicate is the reader's contract. Two shapes look equivalent on the easy
cases::

    # (A) fall through to PASS — flagged
    if exit_code != 0:
        raise TestFailure(...)
    return PASS

    # (B) explicit pass predicate — clean
    if exit_code == 0 and not stderr:
        return PASS
    raise TestFailure("unknown failure mode", ...)

They diverge the moment a third failure mode exists. In (A) a missed check silently
produces PASS: a test escape the Floor Technician sees as a green run. In (B) a missed
check produces an explicit failure with evidence. The framework's failure mode must be
"tests turn red on missing logic", never "tests turn green on missing logic".

Detection is structural, not flow-sensitive: walk from each ``return PASS`` up to the
enclosing function; if no ancestor on that walk is an ``if`` (``elif`` is a nested
``if``) or a ``match`` case arm, fire. Nested functions are separate scopes. Tier-1
counterpart: the child boundary accepts only the ``PASS`` identity and refuses to forge
a verdict from anything else — this rule adds readability, not safety.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from litmus_check.model import Rule, SuiteModule, Violation

_PASS = "PASS"


class GatedPassRule(Rule):
    """Flag a ``return PASS`` that no conditional guards."""

    code = "litmus/gated-pass"
    summary = "return PASS must sit inside an if branch or match arm"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.Return) or not _returns_pass(node):
                continue
            function = module.enclosing_function(node)
            if function is None or self._is_gated(module, node, function):
                continue
            yield module.violation(
                node,
                self.code,
                "unconditional PASS: state the pass condition as an explicit predicate "
                "(`if <pass predicate>: return PASS`) and let every other path raise",
            )

    @staticmethod
    def _is_gated(module: SuiteModule, node: ast.Return, function: ast.AST) -> bool:
        for ancestor in module.ancestors(node):
            if ancestor is function:
                return False
            if isinstance(ancestor, ast.If | ast.match_case):
                return True
        return False


def _returns_pass(node: ast.Return) -> bool:
    value = node.value
    if isinstance(value, ast.Name):
        return value.id == _PASS
    return isinstance(value, ast.Attribute) and value.attr == _PASS
