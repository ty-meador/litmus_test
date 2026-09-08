"""Own strict JSON decoding and packaged schemas shared by the Inspector's readers."""

import json
import math
from dataclasses import dataclass
from functools import cache
from importlib.metadata import distribution
from typing import Literal, cast

from jsonschema import Draft202012Validator

from litmus.run_data import JsonObject, JsonValue


@dataclass(frozen=True, slots=True)
class RecordSchema:
    """One published record contract, shared by readers of the same evidence kind.

    Installation or schema defects raise; they must not be reported as damaged
    operator evidence. Distribution data also resolves in editable installations.
    """

    kind: Literal["artifact", "result", "timeline"]

    def validator(self) -> Draft202012Validator:
        """Check and cache this contract's validator; call only within a reader's worker."""
        return _validator(self.kind)


@cache
def _validator(kind: Literal["artifact", "result", "timeline"]) -> Draft202012Validator:
    # Cache by record kind, never by reader identity: cursor changes must not retain
    # a growing collection of reader instances merely to reuse three fixed schemas.
    path = distribution("litmus").locate_file(f"litmus_data/{kind}-v1.schema.json")
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


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
