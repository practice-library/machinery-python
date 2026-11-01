"""The runtime's JSON codec: contract v1alpha1's wire rules on top of protobuf-py's JSON mapping.

Decoding is strict. The body must be UTF-8 JSON with no key given twice, no NaN or Infinity and no lone
surrogate, and every field must be known: protobuf-py would otherwise drop unknown fields. Proto's own
leniency stays, as the contract allows: proto field names (time_seconds), integers written as strings, and
null for a message, list or map field, which is read as left out.

Encoding writes every envelope field, so ok, data, notRead and warnings are always present (data is null
when the call gives none). A number in data whose value is integral is written as an integer (3, never
3.0): Struct and Value hold doubles, and protobuf-py 0.7.0 writes them with a fraction.
"""
from __future__ import annotations

import json
from typing import Any

from protobuf import Message

from . import failure, rules

ENVELOPE_FIELDS = ("ok", "data", "notRead", "warnings")
MAX_EXACT = 2 ** 53


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in items]
    if len(set(keys)) != len(keys):
        raise ValueError("a key is given twice")
    return dict(items)


def _constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


def strict_load(data: bytes | bytearray) -> Any:
    """The body's JSON value; ValueError unless it is strict JSON."""
    value = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    json.dumps(value, ensure_ascii=False).encode("utf-8")  # a lone surrogate cannot be UTF-8
    return value


def integers(value: Any) -> Any:
    """value with every integral float up to 2^53 as an int."""
    if isinstance(value, float) and value.is_integer() and abs(value) <= MAX_EXACT:
        return int(value)
    if isinstance(value, list):
        return [integers(v) for v in value]
    if isinstance(value, dict):
        return {k: integers(v) for k, v in value.items()}
    return value


def envelope_json(message: Message) -> bytes:
    """A response message as the contract writes it: every envelope field, integral numbers as integers."""
    obj = json.loads(message.to_json(always_emit_implicit=True))
    obj.setdefault("data", None)
    missing = [f for f in ENVELOPE_FIELDS if f not in obj]
    if missing:
        raise ValueError(f"{type(message).__name__} is not an envelope: no {missing[0]}")
    return json.dumps(integers(obj), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class StrictJSONCodec:
    """connectrpc's codec interface, for application/json only."""

    def name(self) -> str:
        return "json"

    def encode(self, message: Message) -> bytes:
        return envelope_json(message)

    def decode(self, data: bytes | bytearray, message_class: type[Message]) -> Message:
        """The request, or invalid_argument with a Failure detail: a decoding error is the runtime's answer
        too. caps.outputBytes is inside the body that did not decode, so the error fits the smallest
        outputBytes the contract allows, and so any the caller sent."""
        try:
            strict_load(data)
            return message_class.from_json(bytes(data), ignore_unknown_fields=False)
        except Exception as e:  # noqa: BLE001 # protobuf-py raises several types
            raise failure.error("invalid_argument", [f"the body is not a valid {message_class.__name__}: {e}"],
                                cap=rules.OUTPUT_BYTES[0]) from e
