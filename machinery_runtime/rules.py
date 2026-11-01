"""The rules of contract v1alpha1's request.schema.json that proto cannot state. The runtime checks them on
the request's canonical JSON form (proto's JSON mapping after decoding, so a value left out is a default)
and answers invalid_argument with the first rule broken."""
from __future__ import annotations

import re
from typing import Any

TIME_SECONDS = (1, 3600)
OUTPUT_BYTES = (1024, 16777216)
STATUS = (100, 599)
HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
START = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
RECORDED_KEYS = {"status", "headers", "body", "error"}
WITH_NETWORK = ("RunUnit", "Run")


def check(call: str, request: dict[str, Any]) -> str | None:
    """Why the request breaks a rule, or None. call is the RPC's name (Describe, Plan, RunUnit, Merge, Run)."""
    caps = request.get("caps")
    if caps is None:
        return "caps is required"
    for key, (low, high) in (("timeSeconds", TIME_SECONDS), ("outputBytes", OUTPUT_BYTES)):
        value = caps.get(key, 0)
        if not low <= value <= high:
            return f"caps.{key} must be {low} to {high}, not {value}"
    if call != "Describe" and "input" not in request:
        return "input is required"
    if call in WITH_NETWORK:
        if "network" not in request:
            return f"network is required on {call}"
        return network(request["network"])
    return None


def network(net: dict[str, Any]) -> str | None:
    hosts = net.get("hosts", [])
    for host in hosts:
        if not HOST.fullmatch(host):
            return f"network.hosts: {host[:80]!r} is not a lower-case host name"
    if "replay" not in net:
        return None
    if hosts:
        return "network has both hosts and replay: replay goes with an empty hosts"
    replay = net["replay"]
    start = replay.get("start", "")
    if not START.fullmatch(start):
        return f"network.replay.start: {start[:40]!r} is not a UTC time as 2026-10-31T12:00:00Z"
    for url, responses in replay.get("exchanges", {}).items():
        for i, recorded in enumerate(responses):
            why = _recorded(recorded)
            if why:
                return f"network.replay.exchanges[{url[:80]!r}][{i}]: {why}"
    return None


def _recorded(rec: Any) -> str | None:
    if not isinstance(rec, dict):
        return "a recorded response is an object"
    extra = sorted(set(rec) - RECORDED_KEYS)
    if extra:
        return f"unknown field {extra[0][:40]!r}"
    if "error" in rec:
        if len(rec) != 1:
            return "error stands alone: no status, headers or body with it"
        if not isinstance(rec["error"], str) or not rec["error"]:
            return "error is a non-empty string"
        return None
    status = rec.get("status")
    if status is None:
        return "a recorded response has a status or an error"
    # a bool is an int in Python, but True and False are out of range
    if not (isinstance(status, (int, float)) and float(status).is_integer() and STATUS[0] <= status <= STATUS[1]):
        return f"status must be an integer from {STATUS[0]} to {STATUS[1]}, not {status!r}"
    headers = rec.get("headers", {})
    if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
        return "headers map names to strings"
    if not isinstance(rec.get("body", ""), str):
        return "body is a string"
    return None
