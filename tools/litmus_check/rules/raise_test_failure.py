"""``litmus/raise-test-failure`` — test methods raise ``TestFailure``, not a plain exception.

A plain ``ValueError`` / ``RuntimeError`` / ``Exception`` thrown from a test method is
classified by the child boundary as a developer bug, not a hardware verdict. The author
almost certainly meant ``TestFailure``: that is the only exception the Floor Technician
reads as Fail-about-the-unit rather than Fail-about-the-author.

Flagged: ``raise <Name>(...)`` (and the un-called ``raise <Name>`` form) inside a
statically known ``LitmusTest`` test method when ``<Name>`` is not ``TestFailure``.
A test method is a class body member passed to ``self.case`` or annotated ``-> Verdict``.
``configure``, ``cleanup``, ``build_suite``, and ``__init__`` are not test methods.

Not flagged: ``raise TestFailure(...)``, a bare ``raise`` (re-raise), or ``raise err``
where the constructor name cannot be read statically. Nested functions inherit the
enclosing method's role: a helper defined inside a test method is still in a test
method.

Tier-1 counterpart: the failure taxonomy classifies any non-``TestFailure`` exception
escaping a test method as a contract-violation bug.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from litmus_check.model import Rule, SuiteModule, Violation

_LIFECYCLE = frozenset({"__init__", "configure", "cleanup", "build_suite"})
_VERDICT = "Verdict"
_TEST_FAILURE = "TestFailure"

type FunctionDef = ast.FunctionDef | ast.AsyncFunctionDef


class RaiseTestFailureRule(Rule):
    """Flag a plain exception raised where ``TestFailure`` was meant."""

    code = "litmus/raise-test-failure"
    summary = "test methods raise TestFailure, not a plain exception"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        for klass in module.litmus_classes():
            test_methods = _test_methods(module, klass)
            for node in ast.walk(klass):
                if not isinstance(node, ast.Raise):
                    continue
                method = module.enclosing_method(node)
                if method is None or method not in test_methods:
                    continue
                name = _raised_constructor_name(node)
                if name is None or name == _TEST_FAILURE:
                    continue
                yield module.violation(
                    node,
                    self.code,
                    f"plain {name} in a test method is classified as a developer bug, "
                    "not a hardware failure. Raise TestFailure instead",
                )


def _test_methods(module: SuiteModule, klass: ast.ClassDef) -> frozenset[FunctionDef]:
    names = _case_method_names(module, klass)
    found: set[FunctionDef] = set()
    for member in klass.body:
        if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if member.name in _LIFECYCLE:
            continue
        if member.name in names or _is_verdict(member.returns):
            found.add(member)
    return frozenset(found)


def _case_method_names(module: SuiteModule, klass: ast.ClassDef) -> frozenset[str]:
    names: set[str] = set()
    for node in ast.walk(klass):
        if not isinstance(node, ast.Call) or module.enclosing_class(node) is not klass:
            continue
        method = module.enclosing_method(node)
        if method is None:
            continue
        receiver = _first_parameter(method)
        if receiver is None or not _is_receiver_call(node.func, receiver, "case"):
            continue
        test = _case_test_argument(node)
        if (
            isinstance(test, ast.Attribute)
            and isinstance(test.value, ast.Name)
            and test.value.id == receiver
        ):
            names.add(test.attr)
    return frozenset(names)


def _case_test_argument(call: ast.Call) -> ast.expr | None:
    if len(call.args) >= 2:
        return call.args[1]
    for keyword in call.keywords:
        if keyword.arg == "test":
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


def _is_verdict(annotation: ast.expr | None) -> bool:
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value.rsplit(".", maxsplit=1)[-1] == _VERDICT
    return _simple_name(annotation) == _VERDICT


def _simple_name(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _raised_constructor_name(node: ast.Raise) -> str | None:
    exc = node.exc
    if exc is None:
        return None
    target: ast.expr = exc.func if isinstance(exc, ast.Call) else exc
    return _simple_name(target)
