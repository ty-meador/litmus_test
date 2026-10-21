"""``litmus/evidence-shadows-ctx`` — an ``@evidence`` key is not a ctx key and not a reserved slot.

A leaf's ctx and its evidence land on one record, so the two namespaces are one: a key
``configure()`` returns names the limit or the target, and a key ``@evidence`` declares
names the reading judged against it. The same word cannot do both. Two of five bench
suites hit the tier-1 refusal on their first run because the natural name for a reading
(``kernel``, ``probe``, ``domain``) was the natural name for what it was judged against
(bench finding F14, issue #161).

Flagged: inside a statically known ``LitmusTest``, a key ``@evidence`` declares (a
string literal, or a field of a same-module ``TypedDict``) that is also a key the class's
``configure()`` is statically known to return (its dict literal, ``dict(k=...)`` call, a
local dict literal it builds, the ``TypedDict`` it is annotated to return, or the keys a
same-module base provides when the class has no ``configure`` of its own), or that is the
framework-owned ``measurements`` slot. Each clashing key is one violation anchored on
the literal that declares it.

Not flagged: a declaration the rule cannot read (a computed key, an imported
``TypedDict``), a class whose ``configure()`` return cannot be read statically, and a
clash with a key only an enclosing suite's ctx provides: a leaf that shares its parent's
context is held to the parent's keys at run time, but a single-file analysis does not
know the parent. Tier 3 is skippable by design; tier 1 is the guard.

Tier-1 counterpart: the child boundary refuses a payload whose key is in the node's ctx
or is ``measurements`` as a ``ContractViolation`` naming the leaf and the key; nothing is
stripped silently.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator

from litmus_check.model import Rule, SuiteModule, Violation
from litmus_check.rules.ctx_provenance import (
    _class_named,  # pyright: ignore[reportPrivateUsage]
    _provided_keys,  # pyright: ignore[reportPrivateUsage]
    _simple_name,  # pyright: ignore[reportPrivateUsage]
)
from litmus_check.rules.evidence_fulfilled import (
    _declaration,  # pyright: ignore[reportPrivateUsage]
)

type FunctionDef = ast.FunctionDef | ast.AsyncFunctionDef

_EVIDENCE = "evidence"
_MEASUREMENTS = "measurements"
_WILDCARD = "*"


class EvidenceShadowsCtxRule(Rule):
    """Flag an ``@evidence`` key that is also a ctx key or the ``measurements`` slot."""

    code = "litmus/evidence-shadows-ctx"
    summary = "no @evidence key is a key configure() returns or the measurements slot"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        """Report an ``@evidence`` key the record already spends on ctx or measurements.

        See the ``litmus/evidence-shadows-ctx`` row in ``docs/litmus-check.md``.
        """
        for klass in module.litmus_classes():
            provided = _provided_keys(module, klass) or frozenset[str]()
            for member in klass.body:
                if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                declared = _declaration(module, member)
                if declared is None:
                    continue
                yield from self._check_method(module, member, declared, provided)

    def _check_method(
        self,
        module: SuiteModule,
        method: FunctionDef,
        declared: tuple[str, ...],
        provided: frozenset[str],
    ) -> Iterator[Violation]:
        # ``_declaration`` read the keys off this decorator, so it is here.
        decorator = next(
            d
            for d in method.decorator_list
            if isinstance(d, ast.Call) and _simple_name(d.func) == _EVIDENCE
        )
        for key in declared:
            anchor = _declaring_node(module, decorator, key)
            if key == _MEASUREMENTS:
                yield module.violation(
                    anchor,
                    self.code,
                    f"{key!r} is the framework-owned slot on the record; evidence may not "
                    "use it. Name the reading for what it is",
                )
            elif key != _WILDCARD and key in provided:
                yield module.violation(
                    anchor,
                    self.code,
                    f"{key!r} is declared by @evidence but configure() returns it as a ctx "
                    "key; ctx and evidence share one record, so a reading may not reuse the "
                    "name of the limit or target it was judged against. Rename the "
                    f"evidence key (e.g. {key}_measured) and leave ctx as it is",
                )


def _declaring_node(module: SuiteModule, decorator: ast.Call, key: str) -> ast.AST:
    """The literal that declares ``key``: the string argument, or the ``TypedDict`` field.

    Falls back to the decorator itself when the literal is not in reach, as for a field
    the ``TypedDict`` inherits from a base.
    """
    for argument in decorator.args:
        if isinstance(argument, ast.Constant) and argument.value == key:
            return argument
    if len(decorator.args) == 1 and isinstance(decorator.args[0], ast.Name):
        klass = _class_named(module, decorator.args[0].id)
        if klass is not None:
            field = _typed_dict_field(klass, key)
            if field is not None:
                return field
    return decorator


def _typed_dict_field(klass: ast.ClassDef, key: str) -> ast.AST | None:
    for statement in klass.body:
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == key
        ):
            return statement.target
    return None
