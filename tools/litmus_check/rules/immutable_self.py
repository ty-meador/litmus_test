"""``litmus/immutable-self`` — ``self`` is written only in ``__init__``.

After construction a ``LitmusTest`` is identity, and identity does not change while a
run is in flight. Runtime state flows out of ``configure()`` into ``utils.ctx``, where
``run_data`` captures it as evidence for free; state hidden on ``self`` is invisible to
that trail and couples lifecycle phases through a side channel.

Flagged, inside any statically known ``LitmusTest`` subclass, for every write whose
target is the enclosing method's receiver parameter, unless that method is ``__init__``
itself (a closure defined inside ``__init__`` runs later and is not the constructor):

- ``self.x = ...``, ``self.x += ...``, ``self.x: T = ...``, including tuple targets;
- ``del self.x``;
- ``for self.x in ...`` and ``with ... as self.x``.

Not flagged: ``self.x.y = ...`` and ``self.x[k] = ...`` (those mutate the contents of
``self.x``, which is the child-list pattern composition relies on), and
``object.__setattr__(self, ...)``, which is the framework's own bypass. Suite code has
no escape hatch — that call in a suite is a design smell for review, not a lint
finding — and the port keeps that decision (#7) where it was made.

Tier-1 counterpart: the metaclass freezes the instance after the most-derived
``__init__`` returns and ordinary assignment raises ``AttributeError`` at runtime.
This rule is the same guard delivered at edit time; it is defence in depth, never the
verdict-integrity boundary.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator

from litmus_check.model import Rule, SuiteModule, Violation

_CONSTRUCTOR = "__init__"


class ImmutableSelfRule(Rule):
    """Flag writes to ``self.<attr>`` outside ``__init__`` in a ``LitmusTest``."""

    code = "litmus/immutable-self"
    summary = "self.<attr> is assigned or deleted only inside __init__"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        for klass in module.litmus_classes():
            for node in ast.walk(klass):
                for target in _write_targets(node):
                    yield from self._check_target(module, node, target)

    def _check_target(
        self, module: SuiteModule, statement: ast.AST, target: ast.expr
    ) -> Iterable[Violation]:
        method = module.enclosing_method(statement)
        if method is None or _is_static_or_class_method(method):
            return
        if not _is_attribute_of(target, _first_parameter(method)):
            return
        if method.name == _CONSTRUCTOR and module.enclosing_function(statement) is method:
            return
        attribute = target.attr if isinstance(target, ast.Attribute) else "?"
        yield module.violation(
            target,
            self.code,
            f"self.{attribute} written outside __init__: self is frozen after construction; "
            "return runtime state from configure() and read it from utils.ctx",
        )


def _write_targets(node: ast.AST) -> Iterator[ast.expr]:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            yield from _flatten(target)
    elif isinstance(node, ast.AugAssign | ast.AnnAssign):
        yield from _flatten(node.target)
    elif isinstance(node, ast.Delete):
        for target in node.targets:
            yield from _flatten(target)
    elif isinstance(node, ast.For | ast.AsyncFor):
        yield from _flatten(node.target)
    elif isinstance(node, ast.With | ast.AsyncWith):
        for item in node.items:
            if item.optional_vars is not None:
                yield from _flatten(item.optional_vars)


def _flatten(target: ast.expr) -> Iterator[ast.expr]:
    if isinstance(target, ast.Tuple | ast.List):
        for element in target.elts:
            yield from _flatten(element)
    elif isinstance(target, ast.Starred):
        yield from _flatten(target.value)
    else:
        yield target


def _is_static_or_class_method(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in method.decorator_list:
        name = decorator.id if isinstance(decorator, ast.Name) else None
        if name in {"staticmethod", "classmethod"}:
            return True
    return False


def _first_parameter(function: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    positional = function.args.posonlyargs + function.args.args
    if not positional:
        return None
    return positional[0].arg


def _is_attribute_of(target: ast.expr, receiver: str | None) -> bool:
    return (
        receiver is not None
        and isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == receiver
    )
