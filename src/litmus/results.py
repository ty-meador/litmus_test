"""Immutable run evidence and the future Inspector's JSON boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import cast, final

from litmus.errors import Failure
from litmus.lifecycle import Slot, State

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

FORMAT_VERSION = 1


def _empty_env() -> Mapping[str, str]:
    return {}


class NodeType(StrEnum):
    """Stable Inspector discriminator for each framework-owned node shape."""

    TEST_SUITE = "test-suite"
    TEST_CASE = "test-case"
    TEST_GROUP = "test-group"
    TEST_SEQUENCE = "test-sequence"


def _failure_json(failure: Failure | None) -> JsonObject | None:
    """Render a framework failure through its total, JSON-safe contract."""
    if failure is None:
        return None
    return cast("JsonObject", failure.to_json())


@final
@dataclass(frozen=True, slots=True)
class SummaryTreeNode:
    """Plain recursive snapshot consumed by a future Inspector.

    ``state is None`` means the node was declared but did not run, as can happen to
    later steps in a failed sequence. It is not a fourth terminal state.
    """

    name: str
    type: NodeType
    state: State | None
    failure: Failure | None = None
    children: tuple[SummaryTreeNode, ...] = ()

    def to_json(self) -> JsonObject:
        """Return the version-independent node shape accepted by ``json.dumps``."""
        return {
            "name": self.name,
            "type": self.type.value,
            "state": None if self.state is None else self.state.value,
            "failure": _failure_json(self.failure),
            "children": [child.to_json() for child in self.children],
        }


@final
@dataclass(frozen=True, slots=True)
class RunResult:
    """Self-contained evidence produced when one test node reaches a terminal state.

    ``slot``, ``value``, and ``swallowed`` preserve the existing internal ``Outcome``
    navigation surface. They are deliberately absent from :meth:`to_json`: raw return
    values are lifecycle machinery, while the serialized form is an evidence contract.
    """

    slot: Slot
    node: str
    state: State
    ancestor_names: list[str]
    bread_crumbs: str
    summary_tree: SummaryTreeNode
    type: NodeType
    failure: Failure | None = None
    value: object = None
    swallowed: BaseException | None = None
    env: Mapping[str, str] = field(default_factory=_empty_env)
    run_data: JsonValue = None

    def __post_init__(self) -> None:
        """Defensively snapshot containers and enforce the navigation invariants."""
        names = list(self.ancestor_names)
        if not names or names[-1] != self.node:
            msg = "ancestor_names must be a non-empty root-to-node path"
            raise ValueError(msg)
        if self.bread_crumbs != "/".join(names):
            msg = "bread_crumbs must equal '/'.join(ancestor_names)"
            raise ValueError(msg)
        if self.summary_tree.name != self.node or self.summary_tree.state is not self.state:
            msg = "summary_tree root must describe the RunResult node and state"
            raise ValueError(msg)
        object.__setattr__(self, "ancestor_names", names)
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))

    @property
    def test_case(self) -> str:
        """Machine-facing alias for the node name used by the JSON contract."""
        return self.node

    @property
    def physical_location(self) -> str:
        """Station identity captured with the run environment."""
        return self.env.get("PHYSICAL_LOCATION", "unknown")

    @property
    def is_bug(self) -> bool:
        """Whether broken code, rather than hardware, produced this result."""
        return self.failure is not None and self.failure.is_bug

    def to_json(self) -> JsonObject:
        """Return the complete, versioned Inspector payload accepted by ``json.dumps``."""
        return {
            "format_version": FORMAT_VERSION,
            "state": self.state.value,
            "failure": _failure_json(self.failure),
            "test_case": self.test_case,
            "type": self.type.value,
            "ancestor_names": list(self.ancestor_names),
            "bread_crumbs": self.bread_crumbs,
            "summary_tree": self.summary_tree.to_json(),
            "physical_location": self.physical_location,
            "env": dict(self.env),
            "run_data": self.run_data,
        }
