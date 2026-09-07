"""``litmus/constructor-hooks`` — surveys and audits are registered in ``__init__``.

A survey or audit is part of the test's identity: stable across runs, visible on the
class, registered once. Registering one from ``configure()`` or a test method makes
the hook set a function of runtime state and hides it from the reader who opens the
constructor to learn what a suite observes.

Flagged: ``self.use_survey(...)`` and ``self.use_audit(...)`` — the receiver being
the enclosing method's first parameter — inside any statically known ``LitmusTest``
subclass when the enclosing method is not ``__init__`` itself. A closure defined in
``__init__`` and called later is not the constructor either.

Tier-1 counterpart: both registrars raise ``ContractViolation`` once the instance is
frozen after the most-derived ``__init__`` returns. This rule moves that error from
the first run to the editor.

The legacy rule also policed ``utils.useShell()`` outside ``configure()``. That half
lives in ``litmus/shell-in-configure`` (``shell_in_configure.py``), added once
``use_shell`` had the runtime contract section 3.4 requires a tier-3 rule to mirror.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from litmus_check.model import Rule, SuiteModule, Violation

_CONSTRUCTOR = "__init__"
_REGISTRARS = frozenset({"use_survey", "use_audit"})


class ConstructorHooksRule(Rule):
    """Flag ``self.use_survey`` / ``self.use_audit`` calls outside ``__init__``."""

    code = "litmus/constructor-hooks"
    summary = "use_survey and use_audit are called only inside __init__"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        for klass in module.litmus_classes():
            for node in ast.walk(klass):
                if isinstance(node, ast.Call):
                    yield from self._check_call(module, node)

    def _check_call(self, module: SuiteModule, call: ast.Call) -> Iterable[Violation]:
        registrar = _registrar_name(call)
        if registrar is None:
            return
        method = module.enclosing_method(call)
        if method is None:
            return
        receiver = _first_parameter(method)
        func = call.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            return
        if func.value.id != receiver:
            return
        if method.name == _CONSTRUCTOR and module.enclosing_function(call) is method:
            return
        yield module.violation(
            call,
            self.code,
            f"{registrar} called outside __init__: surveys and audits are test identity, "
            "registered in the constructor, not runtime state",
        )


def _registrar_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _REGISTRARS:
        return func.attr
    return None


def _first_parameter(method: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    positional = method.args.posonlyargs + method.args.args
    if not positional:
        return None
    return positional[0].arg
