"""``litmus/shell-in-configure`` — a route is declared in ``configure()``, nowhere else.

``use_shell`` with arguments is a declaration: it decides where this node's
``utils.exec`` lands, and the framework writes that decision to the timeline as one
``exec routing:`` line when ``configure()`` returns. A declaration from a test method,
``cleanup``, a survey, or an audit would move the route after that line was written,
so the Floor Technician would read a route the later commands never used.

Flagged: ``<receiver>.use_shell(...)`` called with at least one argument inside a
statically known ``LitmusTest`` subclass when the enclosing method is not
``configure`` itself. A closure defined in ``configure`` and called later is not
``configure`` either. The receiver is not checked by name: ``utils`` is the
convention, not the rule, and a helper may take the service object under any name.

Not flagged: the bare ``use_shell()`` read, which may run anywhere, and ``get_shell``,
which opens an auxiliary endpoint without touching the route.

Tier-1 counterpart: ``TestUtils.use_shell`` raises ``ContractViolation`` for a
declaring call (``host``, ``via``, ``inherit=False``, ``propagate=False``) from any slot
but ``configure``. This rule moves that error from the first run to the editor. It
reads "any argument" where the runtime reads "any route-shaping argument" because a
credential passed without a host is a mistake in either reading.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from litmus_check.model import Rule, SuiteModule, Violation

_CONFIGURE = "configure"
_DECLARER = "use_shell"


class ShellInConfigureRule(Rule):
    """Flag ``use_shell(...)`` with arguments outside ``configure``."""

    code = "litmus/shell-in-configure"
    summary = "use_shell declares the exec route only inside configure()"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        for klass in module.litmus_classes():
            for node in ast.walk(klass):
                if isinstance(node, ast.Call) and _declares_route(node):
                    yield from self._check_call(module, node)

    def _check_call(self, module: SuiteModule, call: ast.Call) -> Iterable[Violation]:
        method = module.enclosing_method(call)
        if method is None:
            return
        if method.name == _CONFIGURE and module.enclosing_function(call) is method:
            return
        yield module.violation(
            call,
            self.code,
            "use_shell declared a route outside configure(): the exec route is fixed when "
            "configure() returns so the 'exec routing:' line stays true for the whole "
            "node; declare it there, or call use_shell() with no arguments to read it",
        )


def _declares_route(call: ast.Call) -> bool:
    """A ``use_shell`` call with any argument; the bare call only reads the route."""
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == _DECLARER):
        return False
    return bool(call.args or call.keywords)
