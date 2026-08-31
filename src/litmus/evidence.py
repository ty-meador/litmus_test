"""JSON-safe evidence snapshots produced from authored runtime context."""

import math
from collections.abc import Mapping
from typing import cast

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

_MEASUREMENTS_KEY = "measurements"


class _Unserializable:
    """Sentinel type kept distinct from every valid JSON value, including ``None``."""

    __slots__ = ()


_UNSERIALIZABLE = _Unserializable()


def default_run_data() -> JsonObject:
    """Return a fresh default evidence object with its reserved design slots."""
    return {_MEASUREMENTS_KEY: []}


def _json_snapshot(
    value: object,
    active_containers: set[int],
) -> JsonValue | _Unserializable:
    """Copy one JSON value, rejecting instances, non-finite numbers, and cycles."""
    value_type = type(value)
    if value is None:
        return None
    if value_type is bool:
        return cast(bool, value)
    if value_type is int:
        return cast(int, value)
    if value_type is float:
        number = cast(float, value)
        return number if math.isfinite(number) else _UNSERIALIZABLE
    if value_type is str:
        return cast(str, value)

    if value_type not in (list, dict):
        return _UNSERIALIZABLE

    container_id = id(value)
    if container_id in active_containers:
        return _UNSERIALIZABLE
    active_containers.add(container_id)
    try:
        if value_type is list:
            copied_list: list[JsonValue] = []
            for item in cast(list[object], value):
                copied = _json_snapshot(item, active_containers)
                if isinstance(copied, _Unserializable):
                    return _UNSERIALIZABLE
                copied_list.append(copied)
            return copied_list

        copied_dict: dict[str, JsonValue] = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                return _UNSERIALIZABLE
            copied = _json_snapshot(item, active_containers)
            if isinstance(copied, _Unserializable):
                return _UNSERIALIZABLE
            copied_dict[key] = copied
        return copied_dict
    finally:
        active_containers.remove(container_id)


def snapshot_run_data(ctx: Mapping[str, object]) -> JsonObject:
    """Return a detached JSON-safe snapshot of the serializable top-level ctx entries.

    A top-level entry survives only when its complete value is serializable. Silently
    pruning an invalid nested handle would make the remaining evidence look complete
    when it is not. Context is typed with string keys; a runtime value that violates
    that contract is stripped rather than coerced into a different JSON key.
    """
    snapshot = default_run_data()
    for key, value in ctx.items():
        if type(key) is not str or key == _MEASUREMENTS_KEY:
            continue
        copied = _json_snapshot(value, set())
        if not isinstance(copied, _Unserializable):
            snapshot[key] = copied
    return snapshot
