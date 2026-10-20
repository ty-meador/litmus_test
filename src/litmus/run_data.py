"""JSON-safe run-data snapshots: the ctx view and the evidence a verdict carries."""

import math
import re
from collections.abc import Mapping, Sequence
from typing import cast

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

MEASUREMENTS_KEY = "measurements"

# What a redacted ctx entry publishes in place of its value. It is the same marker
# DeviceApi writes for a header it will not log, so a reader learns one word for
# "present, withheld" across every record surface.
REDACTED = "<redacted>"


def redact_run_data(run_data: JsonObject, rules: Sequence[re.Pattern[str]]) -> None:
    """Replace the value of every key a ``redact_ctx`` rule matches with ``REDACTED``.

    A rule must match the whole key, so ``"sid"`` withholds ``sid`` and not
    ``inside_temp``. The key stays in the record because a key that silently vanished
    would read as a value never taken. The framework-owned measurements slot is not
    ctx and is never a candidate.
    """
    for key in run_data:
        if key != MEASUREMENTS_KEY and any(rule.fullmatch(key) for rule in rules):
            run_data[key] = REDACTED


class _Unserializable:
    """Sentinel type kept distinct from every valid JSON value, including ``None``."""

    __slots__ = ()


_UNSERIALIZABLE = _Unserializable()


def default_run_data() -> JsonObject:
    """Return a fresh default evidence object with its reserved design slots."""
    return {MEASUREMENTS_KEY: []}


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
        if type(key) is not str or key == MEASUREMENTS_KEY:
            continue
        copied = _json_snapshot(value, set())
        if not isinstance(copied, _Unserializable):
            snapshot[key] = copied
    return snapshot


def invalid_evidence_key(payload: Mapping[str, object]) -> str | None:
    """Return the first key whose value cannot be carried as evidence, or ``None``.

    Evidence is checked entry by entry at the child boundary so the violation names the
    key; nothing is stripped. A non-string key is reported as its ``repr`` because the
    payload is typed with string keys and the offence is the key itself.
    """
    for key, value in payload.items():
        if type(key) is not str:
            return repr(key)
        if isinstance(_json_snapshot(value, set()), _Unserializable):
            return key
    return None


def json_evidence(payload: Mapping[str, object]) -> JsonObject:
    """Copy an already-validated evidence payload into a detached JSON object."""
    copied: JsonObject = {}
    for key, value in payload.items():
        snapshot = _json_snapshot(value, set())
        if isinstance(
            snapshot, _Unserializable
        ):  # pragma: no cover - guarded by invalid_evidence_key
            continue
        copied[key] = snapshot
    return copied
