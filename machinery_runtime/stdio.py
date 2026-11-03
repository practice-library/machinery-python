"""Serves one call of a plugin over stdin and stdout: the stdio binding of contract v1alpha1 (ruling D1).

The host runs the plugin once per call:

  stdin   one line {"path": "/machinery.plugin.v1alpha1.PluginService/<Call>", "timeoutMs": N}, then the
          call's Connect JSON request body, to the end of stdin;
  stdout  one line {"status": "ok"} or {"status": "<Connect code>"}, then the response body, or the Connect
          error body;
  exit    0 when the status is ok, 1 otherwise. The status line is the authority, not the exit code.

The call goes through the same WSGI application the loopback server runs, so decoding, the rules, the
deadline, the output cap, integral numbers and every Failure detail are exactly the loopback binding's. The
frame's timeoutMs becomes the call's Connect-Timeout-Ms. The host ends the process at timeoutMs plus a grace
period (ruling D4); the runtime has answered deadline_exceeded by then, and a plugin thread left running dies
with the process.

A frame that cannot be read is invalid_argument, answered as a transport answers: a Connect error body with no
Failure detail, since no call was made. An HTTP answer that is not a Connect error (404 for a path not served)
is written as the Connect error a Connect client reads from it.
"""
from __future__ import annotations

import io
import json
import sys
from typing import Any, BinaryIO

from . import codec
from .server import application

FRAME_KEYS = {"path", "timeoutMs"}
MAX_TIMEOUT_MS = 10 ** 10  # Connect-Timeout-Ms is at most 10 digits
# The Connect code a Connect client reads from an HTTP status that carries no Connect error (Connect's protocol,
# "HTTP to Error Code"); any other status is unknown.
HTTP_CODES = {400: "internal", 401: "unauthenticated", 403: "permission_denied", 404: "unimplemented",
              429: "unavailable", 502: "unavailable", 503: "unavailable", 504: "unavailable"}


def _frame(line: bytes) -> tuple[str, int] | str:
    """The frame's path and timeoutMs, or why the frame cannot be read."""
    try:
        frame = codec.strict_load(line)
    except ValueError as e:
        return f"the frame is not JSON: {e}"
    if not isinstance(frame, dict) or set(frame) != FRAME_KEYS:
        return f"the frame is an object with exactly {sorted(FRAME_KEYS)}"
    path, timeout = frame["path"], frame["timeoutMs"]
    if not isinstance(path, str) or not path.startswith("/"):
        return "the frame's path is a string starting with /"
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout < MAX_TIMEOUT_MS:
        return f"the frame's timeoutMs is an integer from 1 to {MAX_TIMEOUT_MS - 1}"
    return path, timeout


def _error(code: str, message: str) -> bytes:
    return json.dumps({"code": code, "message": message}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def call(plugin: Any, data: bytes) -> tuple[str, bytes]:
    """The status and body of one call, given everything the host wrote on stdin."""
    line, sep, body = data.partition(b"\n")
    if not sep:
        return "invalid_argument", _error("invalid_argument", "stdin has no frame line")
    frame = _frame(line)
    if isinstance(frame, str):
        return "invalid_argument", _error("invalid_argument", frame)
    path, timeout = frame
    environ = {
        "REQUEST_METHOD": "POST", "PATH_INFO": path, "SCRIPT_NAME": "", "QUERY_STRING": "",
        "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(body)),
        "HTTP_CONNECT_TIMEOUT_MS": str(timeout), "HTTP_CONNECT_PROTOCOL_VERSION": "1",
        "SERVER_NAME": "stdio", "SERVER_PORT": "0", "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0), "wsgi.url_scheme": "http", "wsgi.input": io.BytesIO(body),
        "wsgi.errors": sys.stderr, "wsgi.multithread": False, "wsgi.multiprocess": False, "wsgi.run_once": True,
    }
    answered: dict[str, int] = {}

    def start_response(status, headers, exc_info=None):
        answered["status"] = int(status.split(" ", 1)[0])

    chunks = application(plugin)(environ, start_response)
    try:
        out = b"".join(chunks)
    finally:
        close = getattr(chunks, "close", None)
        if close:
            close()
    status = answered["status"]
    if status == 200:
        return "ok", out
    try:
        code = json.loads(out)["code"]
        if isinstance(code, str) and code:
            return code, out
    except (ValueError, TypeError, KeyError):
        pass
    code = HTTP_CODES.get(status, "unknown")
    return code, _error(code, f"HTTP {status}")


def serve_stdio(plugin: Any, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    """Answers the one call on stdin; returns the exit status (0 ok, 1 an error)."""
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout.buffer
    status, body = call(plugin, stdin.read())
    stdout.write(json.dumps({"status": status}).encode("utf-8") + b"\n" + body)
    stdout.flush()
    return 0 if status == "ok" else 1
