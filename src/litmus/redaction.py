"""Redaction of ctx values a suite marks not-for-the-record (issue #154).

A suite declares ``redact_ctx`` on its class: a sequence of ctx key names, exact or as
regular expressions. A key that matches is a secret the claims need and the record
must not carry. The framework holds the secret to the same standard it holds an SSH
credential to (``docs/contracts.md``, Evidence Contract): the value never reaches the
scoped log, ``run_data``, ``RunResult.to_json()``, a failure's rendering, or any file
in the run directory.

Two mechanisms do that work. The ctx view replaces a matching key's value with
:data:`REDACTED` by name, without inspecting the value. Everything else is masked by
value: a :class:`Redactions` registry, one per run, learns each secret string as it
enters a context and replaces every occurrence of it on every record surface. The
registry is run-wide because the record is run-wide; a secret is secret wherever it
appears, not only under the key that declared it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, cast, final

REDACTED = "<REDACTED>"
"""What the record shows in place of a redacted value."""


def compile_redact_ctx(
    owner: str,
    declared: object,
    *,
    inherited: Sequence[re.Pattern[str]] = (),
) -> tuple[re.Pattern[str], ...]:
    """Validate a class's ``redact_ctx`` declaration and compile its entries.

    Each entry is a ``str`` matched against a ctx key whole: an exact name matches
    itself, and a regular expression matches every key it fully matches. A compiled
    ``re.Pattern`` is taken as written, so an author can pass flags. A ``str`` on its
    own, an empty or non-string entry, or an entry that is not a valid regular
    expression is a ``TypeError`` at class definition, before any instance exists.

    ``inherited`` is what the bases declared. A subclass adds to it and never
    replaces it: an ordinary attribute override would quietly put a base's secret
    back on the record, and redaction fails closed.
    """
    if isinstance(declared, str) or not isinstance(declared, Sequence | set | frozenset):
        msg = (
            f"{owner}.redact_ctx must be a sequence of ctx key names or regular "
            f'expressions, not {type(declared).__name__}; write redact_ctx = ("sid",)'
        )
        raise TypeError(msg)
    patterns: list[re.Pattern[str]] = list(inherited)
    for entry in cast("Sequence[object] | set[object] | frozenset[object]", declared):
        kind = type(entry).__name__
        if isinstance(entry, re.Pattern):
            compiled = cast("re.Pattern[Any]", entry)
            if isinstance(compiled.pattern, str):
                patterns.append(cast("re.Pattern[str]", compiled))
                continue
        if not isinstance(entry, str) or not entry:
            msg = (
                f"{owner}.redact_ctx entries must be non-empty str, not {kind}; "
                "name the ctx key exactly or as a regular expression"
            )
            raise TypeError(msg)
        try:
            patterns.append(re.compile(entry))
        except re.error as error:
            msg = (
                f"{owner}.redact_ctx entry {entry!r} is not a valid regular expression "
                f"({error}); escape it with re.escape() to match the key exactly"
            )
            raise TypeError(msg) from error
    return tuple(dict.fromkeys(patterns))


def key_is_redacted(key: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    """True when ``key`` is one of the declared names or fully matches a pattern."""
    return any(pattern.pattern == key or pattern.fullmatch(key) is not None for pattern in patterns)


@final
class Redactions:
    """The secret values of one run, and the masking that keeps them off the record.

    A value is registered when it enters a context under a redacted key. Only a
    non-empty ``str`` is a needle: a number or a handle under a redacted key is still
    shown as :data:`REDACTED` in the ctx view, but there is no text to hunt for
    elsewhere. Longer secrets are replaced first so a secret that contains another is
    masked whole.
    """

    __slots__ = ("_values",)

    def __init__(self) -> None:
        """Start with nothing to hide."""
        self._values: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        """True once at least one secret is registered."""
        return bool(self._values)

    def register(self, value: object) -> None:
        """Learn one secret value; anything that is not a non-empty ``str`` is ignored."""
        if not isinstance(value, str) or not value or value in self._values:
            return
        self._values = tuple(sorted((*self._values, value), key=len, reverse=True))

    def mask(self, text: str) -> str:
        """Return ``text`` with every registered secret replaced by :data:`REDACTED`."""
        for value in self._values:
            if value in text:
                text = text.replace(value, REDACTED)
        return text

    def mask_json(self, value: Any) -> Any:
        """Return a copy of a JSON-shaped value with every secret string masked.

        Strings are masked wherever they sit: as values, inside lists, inside nested
        mappings. Keys and non-string scalars pass through untouched, as does any
        object that is not a list, tuple, mapping, or string.
        """
        if not self._values:
            return value
        if isinstance(value, str):
            return self.mask(value)
        if isinstance(value, list | tuple):
            return [self.mask_json(item) for item in cast("Sequence[object]", value)]
        if isinstance(value, Mapping):
            items = cast("Mapping[object, object]", value).items()
            return {key: self.mask_json(item) for key, item in items}
        return value
