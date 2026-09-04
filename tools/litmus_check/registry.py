"""Rule discovery and selection."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Iterable, Iterator

from litmus_check import rules as rules_package
from litmus_check.model import Rule


class RuleRegistry:
    """The ordered set of rules a check runs.

    Contract:
      - :meth:`discover` imports every module under :mod:`litmus_check.rules` and
        instantiates each concrete :class:`Rule` subclass it defines. Rules are
        ordered by code so output is stable regardless of import order.
      - :meth:`select` and :meth:`ignore` narrow by code and raise ``KeyError`` on a
        code no rule carries, so a typo in a CI invocation fails loudly instead of
        silently disabling nothing.
    """

    def __init__(self, rules: Iterable[Rule]) -> None:
        by_code: dict[str, Rule] = {}
        for rule in rules:
            if rule.code in by_code:
                msg = f"duplicate rule code: {rule.code}"
                raise ValueError(msg)
            by_code[rule.code] = rule
        self._rules: tuple[Rule, ...] = tuple(by_code[code] for code in sorted(by_code))

    @classmethod
    def discover(cls) -> RuleRegistry:
        """Load every rule shipped under :mod:`litmus_check.rules`."""
        found: list[Rule] = []
        for info in pkgutil.iter_modules(rules_package.__path__):
            module = importlib.import_module(f"{rules_package.__name__}.{info.name}")
            for _, member in inspect.getmembers(module, inspect.isclass):
                if member.__module__ != module.__name__:
                    continue
                if issubclass(member, Rule) and not inspect.isabstract(member):
                    found.append(member())
        return cls(found)

    def __iter__(self) -> Iterator[Rule]:
        return iter(self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    def codes(self) -> tuple[str, ...]:
        """The codes of every rule, in output order."""
        return tuple(rule.code for rule in self._rules)

    def select(self, codes: Iterable[str]) -> RuleRegistry:
        """Keep only the named rules."""
        wanted = self._known(codes)
        return RuleRegistry(rule for rule in self._rules if rule.code in wanted)

    def ignore(self, codes: Iterable[str]) -> RuleRegistry:
        """Drop the named rules."""
        unwanted = self._known(codes)
        return RuleRegistry(rule for rule in self._rules if rule.code not in unwanted)

    def _known(self, codes: Iterable[str]) -> frozenset[str]:
        requested = frozenset(codes)
        unknown = requested - frozenset(self.codes())
        if unknown:
            msg = f"unknown rule code(s): {', '.join(sorted(unknown))}"
            raise KeyError(msg)
        return requested
