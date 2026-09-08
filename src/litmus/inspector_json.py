"""Decode JSON consistently at the Inspector's saved-evidence reader boundaries."""

import json
import math
from typing import cast

from litmus.run_data import JsonObject, JsonValue


class JsonDecoder:
    """Decode UTF-8 evidence without choosing among ambiguous keys or inventing numbers."""

    def decode(self, raw: bytes) -> JsonValue:
        """Return JSON values; malformed, ambiguous, or non-finite input raises ValueError.

        RecursionError remains distinct for readers to diagnose excessively deep input.
        Schema and version selection belong to the reader of the specific record type.
        """
        return cast(
            JsonValue,
            json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
            ),
        )


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    """Refuse ambiguous evidence instead of silently keeping the last duplicate key."""
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> JsonValue:
    raise ValueError(f"non-JSON numeric constant: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite JSON number: {value}")
    return number
