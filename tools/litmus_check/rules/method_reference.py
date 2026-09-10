"""``litmus/method-reference`` — pass a method reference to ``test()``, not a wrapper.

``self.test(name, self._claim)`` is the idiom. A lambda or a nested def in that
second argument is a wrapper the framework already makes unnecessary: method
references passed to ``test`` are auto-bound, so ``lambda utils: self._claim(utils)``
is the same call with more surface area and a worse traceback.

Flagged: a ``lambda`` or a name that refers to a nested def, passed as the test
argument of ``self.test(...)`` (positional or ``fn=``) inside a statically known
``LitmusTest`` subclass.

Not flagged: ``self.test(name, self._claim)``, a module-level function passed by
name, or a call that is not ``self.test``. Multi-statement wrappers are still
flagged — extra logic belongs in a real method, not in ``build_suite``.

Tier-2 counterpart: method references passed to ``test`` are auto-bound; the author
does not wrap them to capture ``self``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from litmus_check.model import Rule, SuiteModule, Violation

type FunctionDef = ast.FunctionDef | ast.AsyncFunctionDef


class MethodReferenceRule(Rule):
    """Flag a lambda or nested def passed to ``self.test``."""

    code = "litmus/method-reference"
    summary = "pass a method reference to test(), not a lambda or nested def"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        """Report a lambda or nested def passed to ``self.test``.

        See the ``litmus/method-reference`` row in ``docs/litmus-check.md``.
        """
        for klass in module.litmus_classes():
            for node in ast.walk(klass):
                if not isinstance(node, ast.Call):
                    continue
                method = module.enclosing_method(node)
                if method is None:
                    continue
                receiver = _first_parameter(method)
                if receiver is None or not _is_receiver_call(node.func, receiver, "test"):
                    continue
                test = _leaf_fn_argument(node)
                if test is None or not _is_wrapper(module, method, test):
                    continue
                yield module.violation(
                    test,
                    self.code,
                    "lambda or nested def passed to test(); pass the method reference "
                    "directly. The framework auto-binds self for instance method refs",
                )


def _is_wrapper(module: SuiteModule, method: FunctionDef, test: ast.expr) -> bool:
    if isinstance(test, ast.Lambda):
        return True
    if not isinstance(test, ast.Name):
        return False
    for node in ast.walk(method):
        is_def = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        if not is_def or node is method or node.name != test.id:
            continue
        if module.enclosing_function(node) is method:
            return True
    return False


def _leaf_fn_argument(call: ast.Call) -> ast.expr | None:
    if len(call.args) >= 2:
        return call.args[1]
    for keyword in call.keywords:
        if keyword.arg == "fn":
            return keyword.value
    return None


def _first_parameter(function: FunctionDef) -> str | None:
    positional = function.args.posonlyargs + function.args.args
    if not positional:
        return None
    return positional[0].arg


def _is_receiver_call(node: ast.expr, receiver: str, method: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == method
        and isinstance(node.value, ast.Name)
        and node.value.id == receiver
    )
