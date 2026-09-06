"""``litmus/unannotated-interface`` — suite interfaces carry complete type hints.

The framework is checked by pyright strict and Ruff's annotation rules, but authored
suites may live outside that toolchain. This rule applies the same visible-interface
standard to functions and methods whose nearest enclosing class is a statically known
``LitmusTest`` subclass. The implicit receiver of instance and class methods is the one
exception; static methods and every other parameter remain explicit.

Methods passed to ``self.test(...)`` are verdict boundaries, not merely annotated
coroutines. Their return annotation must be ``Verdict`` (bare or module-qualified), so
the authoring contract remains legible even when the surrounding project is not type
checked.

This is tier-3 authoring guidance. Runtime hardware and verdict integrity remain with
the child boundary and structural checks; skipping this rule cannot weaken them.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator

from litmus_check.model import Rule, SuiteModule, Violation

_VERDICT_TYPE = "Verdict"

type FunctionDef = ast.FunctionDef | ast.AsyncFunctionDef


class UnannotatedInterfaceRule(Rule):
    """Flag missing suite annotations and non-``Verdict`` test returns."""

    code = "litmus/unannotated-interface"
    summary = "suite interfaces are fully annotated and test methods return Verdict"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        for klass in module.litmus_classes():
            test_methods = _test_methods(module, klass)
            for function in _functions_owned_by(module, klass):
                yield from self._check_parameters(module, klass, function)
                yield from self._check_return(module, function, test_methods)

    def _check_parameters(
        self,
        module: SuiteModule,
        klass: ast.ClassDef,
        function: FunctionDef,
    ) -> Iterable[Violation]:
        for parameter in _parameters_requiring_annotations(klass, function):
            if parameter.annotation is None:
                yield module.violation(
                    parameter,
                    self.code,
                    f"{function.name} parameter '{parameter.arg}' requires a type annotation",
                )

    def _check_return(
        self,
        module: SuiteModule,
        function: FunctionDef,
        test_methods: frozenset[FunctionDef],
    ) -> Iterable[Violation]:
        if function in test_methods:
            if not _is_verdict(function.returns):
                yield module.violation(
                    function,
                    self.code,
                    f"test method {function.name} must declare -> Verdict",
                )
            return
        if function.returns is None:
            yield module.violation(
                function,
                self.code,
                f"{function.name} requires a return type annotation",
            )


def _functions_owned_by(module: SuiteModule, klass: ast.ClassDef) -> Iterator[FunctionDef]:
    for node in ast.walk(klass):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and module.enclosing_class(node) is klass
        ):
            yield node


def _parameters_requiring_annotations(
    klass: ast.ClassDef,
    function: FunctionDef,
) -> Iterator[ast.arg]:
    positional = [*function.args.posonlyargs, *function.args.args]
    is_direct_method = function in klass.body
    is_static = any(
        _decorator_name(decorator) == "staticmethod" for decorator in function.decorator_list
    )
    if is_direct_method and not is_static and positional:
        positional = positional[1:]

    yield from positional
    yield from function.args.kwonlyargs
    if function.args.vararg is not None:
        yield function.args.vararg
    if function.args.kwarg is not None:
        yield function.args.kwarg


def _test_methods(module: SuiteModule, klass: ast.ClassDef) -> frozenset[FunctionDef]:
    names: set[str] = set()
    for node in ast.walk(klass):
        if not isinstance(node, ast.Call) or module.enclosing_class(node) is not klass:
            continue
        method = module.enclosing_method(node)
        if method is None:
            continue
        receiver = _first_parameter(method)
        if receiver is None or not _is_receiver_call(node.func, receiver, "test"):
            continue
        test = _leaf_fn_argument(node)
        if (
            isinstance(test, ast.Attribute)
            and isinstance(test.value, ast.Name)
            and test.value.id == receiver
        ):
            names.add(test.attr)
    return frozenset(
        node
        for node in klass.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    )


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


def _is_verdict(annotation: ast.expr | None) -> bool:
    if isinstance(annotation, ast.Subscript):
        # ``-> Verdict[MyEvidence]`` names the evidence shape (issue #118).
        annotation = annotation.value
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        bare = annotation.value.split("[", 1)[0]
        return bare.rsplit(".", maxsplit=1)[-1] == _VERDICT_TYPE
    return _decorator_name(annotation) == _VERDICT_TYPE


def _decorator_name(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None
