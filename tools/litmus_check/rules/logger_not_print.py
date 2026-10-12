"""``litmus/logger-not-print`` — suite code logs through ``utils.logger``, never ``print``.

``utils.logger`` is scoped to the node's root-to-leaf path and every entry it writes
lands in the run's timeline, the authoritative chronology the Inspector and the Floor
Technician read. ``print`` writes to the process's stdout, which the framework does
not capture: the line is gone the moment the terminal scrolls, and it is absent from
the artifact directory the FT opens after the fact.

Flagged: a call to the builtin ``print`` inside a statically known ``LitmusTest``
subclass, in any method.

Not flagged: ``print`` outside a ``LitmusTest`` class (a ``__main__`` block or a
harness script has no logger to use), or a method named ``print`` on some other object.

Tier-1 counterpart: none. The framework cannot intercept the builtin without changing
what ``print`` means everywhere, so the rule is the gate.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from litmus_check.model import Rule, SuiteModule, Violation


class LoggerNotPrintRule(Rule):
    """Flag ``print(...)`` inside a ``LitmusTest`` subclass."""

    code = "litmus/logger-not-print"
    summary = "log through utils.logger so the entry reaches the timeline; print reaches nothing"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        """Report every ``print`` call inside a suite class.

        See the ``litmus/logger-not-print`` row in ``docs/litmus-check.md``.
        """
        for klass in module.litmus_classes():
            for node in ast.walk(klass):
                if not isinstance(node, ast.Call):
                    continue
                if not (isinstance(node.func, ast.Name) and node.func.id == "print"):
                    continue
                yield module.violation(
                    node,
                    self.code,
                    "print() writes to stdout, which the timeline never sees; log through "
                    "utils.logger so the entry sits in the run's chronology beside the "
                    "verdict it explains",
                )
