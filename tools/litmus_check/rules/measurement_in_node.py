"""``litmus/measurement-in-node`` — a reading is recorded where a node is running.

A measurement belongs to the node whose ``configure()`` or test method is executing:
that is the record it lands on, the verdict it sits beside, and the series the consumer
keys on. ``cleanup`` runs after the node settled; a survey or audit hook runs beside a
node, not as one; the root-owned ``before_each``/``after_each`` hooks run between
nodes; module scope has no node at all. The framework refuses each at runtime; this
rule names the slot in the editor, where the developer can move the call.

Flagged: a ``measure(...)`` call, either spelling (``utils.measure`` or the package-root
``measure``; the receiver is not checked by name), when it sits

- in ``cleanup`` of a statically known ``LitmusTest`` subclass;
- in a method that class registers with ``self.use_survey`` or ``self.use_audit``;
- in a function named ``before_each`` or ``after_each``, or one passed under either
  keyword to a call (a ``run(before_each=...)``), including a lambda;
- at module or class scope, outside any function.

A closure defined in one of those slots is still in that slot.

Not flagged: ``configure``, a test method, and any helper whose caller the rule cannot
see; a helper called from ``cleanup`` is the runtime guard's to catch. Tier 3 is
skippable by design and tier 1 is the guard.

Tier-1 counterpart: the outside-a-node guard raises ``ContractViolation`` naming the
node and the measurement for ``litmus.measure`` with nothing bound and for either
spelling from ``cleanup``, a survey, an audit, a hook, or after the node settled.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator

from litmus_check.model import FunctionNode, Rule, SuiteModule, Violation
from litmus_check.rules.measurement_name_unique import is_measure

_CLEANUP = "cleanup"
_REGISTRARS = frozenset({"use_survey", "use_audit"})
_RUN_HOOKS = frozenset({"before_each", "after_each"})
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


class MeasurementInNodeRule(Rule):
    """Flag ``measure(...)`` from any slot that is not a running node."""

    code = "litmus/measurement-in-node"
    summary = "measure() is called from configure() or a test method, where a node is running"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        hooks = _run_hooks(module)
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call) and is_measure(node):
                slot = self._slot(module, node, hooks)
                if slot is not None:
                    yield module.violation(
                        node,
                        self.code,
                        f"measure() called {slot}: a reading belongs to the node whose "
                        "configure() or test method is running, and tier 1 refuses it "
                        "anywhere else; move the call into the node",
                    )

    def _slot(
        self, module: SuiteModule, call: ast.Call, hooks: frozenset[FunctionNode]
    ) -> str | None:
        """Name the slot the call sits in when that slot is not a node, else ``None``."""
        functions = [
            ancestor for ancestor in module.ancestors(call) if isinstance(ancestor, _FUNCTIONS)
        ]
        if not functions:
            return "outside any function"
        if any(function in hooks or _is_named_hook(function) for function in functions):
            return "in a before_each/after_each hook"
        method = module.enclosing_method(call)
        klass = module.enclosing_class(call)
        if method is None or klass is None or not module.is_litmus_class(klass):
            return None
        if method.name == _CLEANUP:
            return "in cleanup"
        if method.name in module.registered_methods(klass, _REGISTRARS):
            return "in a survey or audit hook"
        return None


def _is_named_hook(function: FunctionNode) -> bool:
    return isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef) and (
        function.name in _RUN_HOOKS
    )


def _run_hooks(module: SuiteModule) -> frozenset[FunctionNode]:
    """Functions handed to a call under ``before_each=`` / ``after_each=``, by name or inline."""
    named: set[str] = set()
    inline: set[FunctionNode] = set()
    for node in ast.walk(module.tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg not in _RUN_HOOKS:
                continue
            if isinstance(keyword.value, ast.Lambda):
                inline.add(keyword.value)
            elif isinstance(keyword.value, ast.Name):
                named.add(keyword.value.id)
            elif isinstance(keyword.value, ast.Attribute):
                named.add(keyword.value.attr)
    return frozenset(inline) | frozenset(_functions_named(module, named))


def _functions_named(module: SuiteModule, names: set[str]) -> Iterator[FunctionNode]:
    for node in ast.walk(module.tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in names:
            yield node
