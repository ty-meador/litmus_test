"""``litmus/evidence-fulfilled`` — every ``return PASS({...})`` carries what ``@evidence`` declared.

``@evidence("k", ...)`` on a test method is its return signature: the keys a pass must
carry. The declaration is static and lives beside the code that keeps it, so the
promise can be checked where it is written (issue #118).

Flagged: inside a method of a statically known ``LitmusTest`` decorated with
``@evidence`` whose declaration can be read (string-literal keys, or a same-module
``TypedDict`` class), a ``return PASS`` with no payload, and a ``return PASS({...})``
whose dict literal (or ``dict(k=...)`` call) lacks a declared key. Each missing key is
one violation naming it.

Not flagged: a payload that is not a literal (a local name, a call, a ``**spread``), a
declaration the rule cannot read (a computed key, an imported ``TypedDict``, a
``total=False`` one), and returns inside nested functions. Tier 3 is skippable by
design; tier 1 holds every ``PASS`` to the declaration at run time.

Tier-1 counterpart: a ``PASS`` that does not carry a declared key is a
``ContractViolation`` naming the leaf and the key; the node lands in Fail as a
developer bug and no hardware verdict is forged.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator

from litmus_check.model import Rule, SuiteModule, Violation
from litmus_check.rules.ctx_provenance import (
    _class_named,  # pyright: ignore[reportPrivateUsage]
    _literal_dict_keys,  # pyright: ignore[reportPrivateUsage]
    _simple_name,  # pyright: ignore[reportPrivateUsage]
    _typed_dict_class_fields,  # pyright: ignore[reportPrivateUsage]
)

type FunctionDef = ast.FunctionDef | ast.AsyncFunctionDef

_EVIDENCE = "evidence"
_PASS = "PASS"
_DICT = "dict"
_TOTAL = "total"


class EvidenceFulfilledRule(Rule):
    """Flag a ``return PASS`` that does not carry every key ``@evidence`` declared."""

    code = "litmus/evidence-fulfilled"
    summary = "every return PASS({...}) in an @evidence method carries every declared key"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        """Report a ``return PASS`` that drops a key ``@evidence`` declared.

        See the ``litmus/evidence-fulfilled`` row in ``docs/litmus-check.md``.
        """
        for klass in module.litmus_classes():
            for member in klass.body:
                if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                declared = _declaration(module, member)
                if declared is None:
                    continue
                yield from self._check_method(module, member, declared)

    def _check_method(
        self, module: SuiteModule, method: FunctionDef, declared: tuple[str, ...]
    ) -> Iterator[Violation]:
        for statement in _own_returns(method):
            payload = _pass_payload(statement.value)
            if payload is _NOT_A_PASS or payload is _UNREADABLE:
                continue
            if payload is _BARE:
                names = ", ".join(repr(key) for key in declared)
                yield module.violation(
                    statement,
                    self.code,
                    f"@evidence declares {names} but this PASS carries nothing; "
                    f"return PASS({{...}}) with the readings",
                )
                continue
            assert isinstance(payload, frozenset)
            for key in declared:
                if key not in payload:
                    yield module.violation(
                        statement,
                        self.code,
                        f"{key!r} is declared by @evidence but this PASS does not carry it; "
                        "add it to the payload",
                    )


class _Marker:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


_NOT_A_PASS = _Marker("not a PASS")
_UNREADABLE = _Marker("unreadable payload")
_BARE = _Marker("bare PASS")


def _own_returns(function: FunctionDef) -> Iterator[ast.Return]:
    """``return`` statements in ``function``'s own body, not in nested defs."""
    stack: list[ast.AST] = list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Return):
            yield node
            continue
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _is_pass(node: ast.expr | None) -> bool:
    if isinstance(node, ast.Name):
        return node.id == _PASS
    return isinstance(node, ast.Attribute) and node.attr == _PASS


def _pass_payload(value: ast.expr | None) -> _Marker | frozenset[str]:
    if _is_pass(value):
        return _BARE
    if not isinstance(value, ast.Call) or not _is_pass(value.func):
        return _NOT_A_PASS
    if len(value.args) != 1 or value.keywords:
        return _UNREADABLE
    payload = value.args[0]
    if isinstance(payload, ast.Dict):
        keys = _literal_dict_keys(payload)
        return _UNREADABLE if keys is None else keys
    if isinstance(payload, ast.Call) and _simple_name(payload.func) == _DICT and not payload.args:
        names: set[str] = set()
        for keyword in payload.keywords:
            if keyword.arg is None:
                return _UNREADABLE
            names.add(keyword.arg)
        return frozenset(names)
    return _UNREADABLE


def _declaration(module: SuiteModule, function: FunctionDef) -> tuple[str, ...] | None:
    """The keys ``@evidence`` declares on ``function``, or ``None`` when absent or unreadable."""
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call) or _simple_name(decorator.func) != _EVIDENCE:
            continue
        if decorator.keywords or not decorator.args:
            return None
        if len(decorator.args) == 1 and isinstance(decorator.args[0], ast.Name):
            return _typed_dict_declaration(module, decorator.args[0].id)
        keys: list[str] = []
        for argument in decorator.args:
            if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
                return None
            keys.append(argument.value)
        return tuple(keys)
    return None


def _typed_dict_declaration(module: SuiteModule, name: str) -> tuple[str, ...] | None:
    klass = _class_named(module, name)
    if klass is None:
        return None
    for keyword in klass.keywords:
        if keyword.arg == _TOTAL:
            return None
    fields = _typed_dict_class_fields(module, klass)
    if not fields:
        return None
    return tuple(sorted(fields))
