"""Every error the runtime answers: a Connect error whose message is its warnings joined by "; " and which
carries the envelope (ok false, data null) as one machinery.plugin.v1alpha1.Failure detail, as the
plugin.proto header requires of every error the plugin returns.

The error body fits caps.outputBytes by shortening what it says, never by dropping the detail: notRead goes
first, then every warning but the first, then the end of that warning. Only below the smallest outputBytes
the contract allows (1024), where not even one character fits, are the detail and the message dropped.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Sequence

from connectrpc.code import Code
from connectrpc.errors import ConnectError, ErrorDetail
from machinery.plugin.v1alpha1 import plugin_pb as pb
from protobuf.wkt.google.protobuf.any_pb import Any as AnyMessage

TYPE = "machinery.plugin.v1alpha1.Failure"
CODES = {"invalid_argument": Code.INVALID_ARGUMENT, "failed_precondition": Code.FAILED_PRECONDITION,
         "resource_exhausted": Code.RESOURCE_EXHAUSTED, "unimplemented": Code.UNIMPLEMENTED,
         "unavailable": Code.UNAVAILABLE, "deadline_exceeded": Code.DEADLINE_EXCEEDED, "internal": Code.INTERNAL}
WARNING_MAX, NOT_READ_MAX = 500, 300  # envelope.schema.json, in characters


def check_strings(warnings: Sequence[str], not_read: Sequence[tuple[str, str]]) -> str | None:
    """Why the warnings or not_read pairs break envelope.schema.json's limits, or None."""
    for w in warnings:
        if not isinstance(w, str) or not 1 <= len(w) <= WARNING_MAX:
            return f"a warning is a string of 1 to {WARNING_MAX} characters, not {str(w)[:40]!r}"
    for pair in not_read:
        if not (isinstance(pair, (tuple, list)) and len(pair) == 2
                and all(isinstance(s, str) and 1 <= len(s) <= NOT_READ_MAX for s in pair)):
            return f"a not_read entry is (what, why), each 1 to {NOT_READ_MAX} characters, not {str(pair)[:60]!r}"
    return None


def not_read_json(pairs: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"what": what, "why": why} for what, why in pairs]


def body_size(code: str, message: str, details: Sequence[ErrorDetail]) -> int:
    """The length of the error body connectrpc 0.13.0 writes (its ConnectWireError.to_dict, json.dumps)."""
    body: dict[str, Any] = {"code": code, "message": message}
    if details:
        body["details"] = [{"type": d.type_name, "value": base64.b64encode(d.message_bytes).decode("ascii").rstrip("=")}
                           for d in details]
    return len(json.dumps(body, ensure_ascii=False).encode("utf-8"))


def _parts(warnings: Sequence[str], not_read: Sequence[tuple[str, str]]) -> tuple[str, list[ErrorDetail]]:
    envelope = {"ok": False, "data": None, "notRead": not_read_json(not_read), "warnings": list(warnings)}
    detail = ErrorDetail(AnyMessage.pack(pb.Failure.from_json(json.dumps(envelope))))  # packed: no debug copy
    return "; ".join(warnings), [detail]


def error(code: str, warnings: Sequence[str], not_read: Sequence[tuple[str, str]] = (),
          cap: int | None = None) -> ConnectError:
    """The Connect error for code, with its Failure detail, fitted into cap bytes."""
    warnings = [w[:WARNING_MAX] for w in warnings]
    message, details = _parts(warnings, not_read)
    if cap is not None and body_size(code, message, details) > cap:
        message, details = _parts(warnings, ())
    if cap is not None and body_size(code, message, details) > cap:
        first = warnings[0]
        lo, hi = 0, len(first)  # the longest prefix of the first warning alone that fits, by bisection
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if body_size(code, *_parts([first[:mid]], ())) <= cap:
                lo = mid
            else:
                hi = mid - 1
        message, details = _parts([first[:lo]], ()) if lo else ("", [])
    return ConnectError(CODES[code], message, details=details)
