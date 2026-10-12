"""``litmus/env-via-utils`` — the environment is read through ``utils.env``, never ``os``.

``utils.env`` is the run's own snapshot: what ``RunResult`` records for post-run
auditing (filtered by ``env_allow`` / ``env_deny`` when declared), what a self-test
injects, and what ``utils.env.require`` turns into a ``TestFailure`` when a variable
is unset. A value read from ``os.environ`` is invisible to that record and cannot be
injected, so the run that produced a verdict cannot be reproduced from its evidence.

Flagged: ``os.environ`` in any use (subscript, ``.get``, ``in``), ``os.getenv(...)``,
and the bare names ``environ`` / ``getenv`` when imported from ``os`` (under any
alias), inside a statically known ``LitmusTest`` subclass.

Not flagged: the same reads outside a ``LitmusTest`` class. A harness script or a
self-test may consult the process environment; only suite code has ``utils`` to use.

Tier-1 counterpart: none can exist, since ``os.environ`` is the interpreter's. The rule
is the only gate, which is why it stays narrow and names the replacement.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from litmus_check.model import Rule, SuiteModule, Violation

_OS_NAMES: frozenset[str] = frozenset({"environ", "getenv"})


class EnvViaUtilsRule(Rule):
    """Flag ``os.environ`` and ``os.getenv`` inside a ``LitmusTest`` subclass."""

    code = "litmus/env-via-utils"
    summary = "read the environment through utils.env, never os.environ or os.getenv"

    def check(self, module: SuiteModule) -> Iterable[Violation]:
        """Report every process-environment read inside a suite class.

        See the ``litmus/env-via-utils`` row in ``docs/litmus-check.md``.
        """
        os_aliases, bare_names = _imported_names(module.tree)
        for klass in module.litmus_classes():
            for node in ast.walk(klass):
                spelling = _os_read(node, os_aliases, bare_names)
                if spelling is None:
                    continue
                yield module.violation(
                    node,
                    self.code,
                    f"{spelling} is outside the run's environment snapshot; read it through "
                    "utils.env so the value reaches RunResult and a self-test can inject it "
                    "(utils.env.require raises TestFailure when the variable is unset)",
                )


def _imported_names(tree: ast.Module) -> tuple[frozenset[str], dict[str, str]]:
    """Return the names ``os`` is bound to, and ``from os import`` aliases to read names."""
    os_aliases: set[str] = set()
    bare_names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    os_aliases.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in _OS_NAMES:
                    bare_names[alias.asname or alias.name] = alias.name
    return frozenset(os_aliases), bare_names


def _os_read(node: ast.AST, os_aliases: frozenset[str], bare_names: dict[str, str]) -> str | None:
    """Return the ``os.<name>`` spelling this node reads, or ``None``."""
    if isinstance(node, ast.Attribute) and node.attr in _OS_NAMES:
        if isinstance(node.value, ast.Name) and node.value.id in os_aliases:
            return f"os.{node.attr}"
        return None
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        read = bare_names.get(node.id)
        if read is not None:
            return f"os.{read}"
    return None
