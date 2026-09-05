"""``litmus/pure-build-suite`` — ``build_suite`` is a table of contents, not a program.

A suite declares its composition the way a document declares its outline: one
``return`` of nested ``self.test`` / ``self.sequence`` / ``self.parallel`` calls. That
declarativeness is a defended convention, not something the language makes impossible
to violate, which is exactly why a rule exists for it. Logic in ``build_suite`` hides
which claims a run will make until the run happens; the Floor Technician reading the
summary tree, and the reviewer reading the source, both deserve to see the same list.

Flagged:

- any statement other than the docstring and a single ``return``;
- inside the returned expression: conditionals (``x if c else y``, ``and``/``or``,
  comparisons), comprehensions that carry an ``if`` filter, lambdas, ``await``,
  ``yield``, and walrus assignments.

Not flagged: calls (composition factories, child-suite constructors, name helpers),
f-strings, literals, attribute and subscript reads, unary/binary operators on them,
and an unfiltered comprehension. The enumeration idiom (issue #110) stamps one leaf per
core, rail, or channel from a single method:
``[self.test(f"cpu{i}", self._cpu, cpu=i) for i in range(n)]``.
That maps a known population onto leaves; it does not choose among them. A comprehension
with an ``if`` clause chooses, and is flagged. The rule polices control flow, not
vocabulary — a suite may compose children from a helper or enumerate them; it may not
decide at build time which children exist.

Tier-2 counterpart: ``__init_subclass__`` requires ``build_suite`` to exist with the
right shape; tier 1 refuses anything but a ``LitmusTest`` from it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from litmus_check.model import Rule, SuiteModule, Violation

_BUILD_SUITE = "build_suite"

_LOGIC_EXPRESSIONS: tuple[type[ast.AST], ...] = (
    ast.IfExp,
    ast.BoolOp,
    ast.Compare,
    ast.Lambda,
    ast.Await,
    ast.Yield,
    ast.YieldFrom,
    ast.NamedExpr,
)

_COMPREHENSIONS = (
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


class PureBuildSuiteRule(Rule):
    """Flag statements and control-flow expressions inside ``build_suite``."""

    code = "litmus/pure-build-suite"
    summary = "build_suite returns one composition expression and holds no logic"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        for klass in module.litmus_classes():
            for member in klass.body:
                is_def = isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef)
                if is_def and member.name == _BUILD_SUITE:
                    yield from self._check_body(module, member)

    def _check_body(
        self, module: SuiteModule, function: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> Iterable[Violation]:
        statements = list(function.body)
        if statements and _is_docstring(statements[0]):
            statements = statements[1:]
        returns = [s for s in statements if isinstance(s, ast.Return)]
        for statement in statements:
            if isinstance(statement, ast.Return):
                continue
            yield module.violation(
                statement,
                self.code,
                f"logic in build_suite: {type(statement).__name__} statement; "
                "build_suite is a table of contents — declare the composition in one return",
            )
        for statement in returns[1:]:
            yield module.violation(
                statement, self.code, "logic in build_suite: more than one return"
            )
        for statement in returns[:1]:
            if statement.value is not None:
                yield from self._check_expression(module, statement.value)

    def _check_expression(self, module: SuiteModule, expression: ast.expr) -> Iterable[Violation]:
        for node in ast.walk(expression):
            if isinstance(node, _COMPREHENSIONS) and any(g.ifs for g in node.generators):
                yield module.violation(
                    node,
                    self.code,
                    f"logic in build_suite: {type(node).__name__} filters the composition "
                    "at build time; enumerate a known population, do not choose among it",
                )
                continue
            if isinstance(node, _LOGIC_EXPRESSIONS):
                yield module.violation(
                    node,
                    self.code,
                    f"logic in build_suite: {type(node).__name__} decides the composition "
                    "at build time; the suite must declare its claims, not compute them",
                )


def _is_docstring(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )
