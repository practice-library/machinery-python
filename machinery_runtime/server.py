"""Serves a plugin as machinery.plugin.v1alpha1.PluginService: Connect unary JSON over HTTP/1.1 on loopback.

Per call, the runtime and not the plugin:
  - decodes strictly (codec.py) and checks the rules proto cannot state (rules.py): invalid_argument;
  - answers 404 for a path it does not serve (Connect clients read it as unimplemented), 415 for a body
    that is not application/json, and unimplemented for a call the plugin has no method for;
  - calls the plugin with plain JSON values, and gives up after caps.timeSeconds or Connect-Timeout-Ms,
    whichever is sooner: deadline_exceeded (the plugin's thread is left to finish on its own);
  - writes the answer with every envelope field and integral numbers as integers, and refuses one larger
    than caps.outputBytes: resource_exhausted;
  - turns a Refusal into its Connect code, with the warnings joined by "; " as the message and the envelope
    (ok false, data null) as a machinery.plugin.v1alpha1.Failure detail. Any error body fits
    caps.outputBytes: the detail is dropped first, then the message.
"""
from __future__ import annotations

import base64
import json
import socketserver
import sys
import threading
from dataclasses import dataclass
from typing import Any, Sequence
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

from connectrpc.code import Code
from connectrpc.errors import ConnectError, ErrorDetail
from machinery.plugin.v1alpha1 import plugin_pb as pb
from machinery.plugin.v1alpha1.plugin_connect import PluginServiceWSGIApplication
from protobuf.wkt.google.protobuf.any_pb import Any as AnyMessage

from . import codec, rules

PACKAGE = "machinery.plugin.v1alpha1"
MAX_REQUEST_BYTES = 16 << 20
CODES = {"invalid_argument": Code.INVALID_ARGUMENT, "failed_precondition": Code.FAILED_PRECONDITION,
         "resource_exhausted": Code.RESOURCE_EXHAUSTED, "unimplemented": Code.UNIMPLEMENTED,
         "unavailable": Code.UNAVAILABLE}
# call: (the plugin's method, whether it gets network, the response message)
CALLS = {"Describe": ("describe", False, pb.DescribeResponse), "Plan": ("plan", False, pb.PlanResponse),
         "RunUnit": ("run_unit", True, pb.RunUnitResponse), "Merge": ("merge", False, pb.MergeResponse),
         "Run": ("run", True, pb.RunResponse)}


@dataclass(frozen=True)
class Answer:
    """What a call answers when it succeeds. not_read holds (what, why) pairs."""
    data: Any
    not_read: Sequence[tuple[str, str]] = ()
    warnings: Sequence[str] = ()


class Refusal(Exception):
    """A call that fails: code is one of CODES, with at least one warning saying why."""

    def __init__(self, code: str, warnings: Sequence[str], not_read: Sequence[tuple[str, str]] = ()):
        if code not in CODES:
            raise ValueError(f"a Refusal's code is one of {sorted(CODES)}, not {code!r}")
        if not warnings or not all(isinstance(w, str) and w for w in warnings):
            raise ValueError("a Refusal has at least one warning, each a non-empty string")
        super().__init__("; ".join(warnings))
        self.code, self.warnings, self.not_read = code, list(warnings), list(not_read)


def _not_read(pairs: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"what": what, "why": why} for what, why in pairs]


def _body_size(code: str, message: str, details: list[ErrorDetail]) -> int:
    """The length of the error body connectrpc 0.13.0 writes (its ConnectWireError.to_dict, json.dumps)."""
    body: dict[str, Any] = {"code": code, "message": message}
    if details:
        body["details"] = [{"type": d.type_name, "value": _b64(d.message_bytes)} for d in details]
    return len(json.dumps(body, ensure_ascii=False).encode("utf-8"))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").rstrip("=")


def error(code: str, warnings: Sequence[str], not_read: Sequence[tuple[str, str]] = (),
          cap: int | None = None) -> ConnectError:
    """The Connect error for a refusal, with its Failure detail, fitted into cap bytes."""
    failure = pb.Failure.from_json(json.dumps(
        {"ok": False, "data": None, "notRead": _not_read(not_read), "warnings": list(warnings)}))
    message = "; ".join(warnings)
    details = [ErrorDetail(AnyMessage.pack(failure))]  # packed: connectrpc then adds no debug copy
    if cap is not None and _body_size(code, message, details) > cap:
        details = []
        if _body_size(code, message, details) > cap:
            message = ""
    return ConnectError(CODES[code], message, details=details)


def _within(seconds: float, fn, *args):
    """fn(*args), or deadline_exceeded after seconds; the plugin's own exceptions pass through."""
    box: dict[str, Any] = {}

    def target():
        try:
            box["value"] = fn(*args)
        except BaseException as e:  # noqa: BLE001 # handed to the caller's thread
            box["error"] = e

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise ConnectError(Code.DEADLINE_EXCEEDED, f"the call did not finish within {seconds:g} seconds")
    if "error" in box:
        raise box["error"]
    return box["value"]


class _Service:
    """The generated PluginServiceSync, answering for one plugin."""

    def __init__(self, plugin: Any):
        self.plugin = plugin

    def describe(self, request, ctx):
        return self._call("Describe", request, ctx)

    def plan(self, request, ctx):
        return self._call("Plan", request, ctx)

    def run_unit(self, request, ctx):
        return self._call("RunUnit", request, ctx)

    def merge(self, request, ctx):
        return self._call("Merge", request, ctx)

    def run(self, request, ctx):
        return self._call("Run", request, ctx)

    def _call(self, call: str, request, ctx):
        method, with_network, response_class = CALLS[call]
        req = json.loads(request.to_json())
        caps = req.get("caps") or {}
        cap = caps.get("outputBytes") or None
        why = rules.check(call, req)
        if why:
            raise error("invalid_argument", [why], cap=cap)
        fn = getattr(self.plugin, method, None)
        if fn is None:
            raise ConnectError(Code.UNIMPLEMENTED, f"this plugin does not answer {call}")
        args = [] if call == "Describe" else [req["input"]]
        if with_network:
            net = req["network"]
            args.append(dict({"hosts": []}, **net) if "replay" not in net else
                        {"hosts": [], "replay": dict({"exchanges": {}}, **net["replay"])})
        args.append({"timeSeconds": caps["timeSeconds"], "outputBytes": caps["outputBytes"]})
        seconds = float(caps["timeSeconds"])
        left = ctx.timeout_ms
        if left is not None:
            seconds = min(seconds, max(left, 0) / 1000)
        try:
            answer = _within(seconds, fn, *args)
        except Refusal as r:
            raise error(r.code, r.warnings, r.not_read, cap) from None
        except ConnectError:
            raise
        except Exception as e:  # noqa: BLE001 # the plugin's own bug: internal, naming only its type
            raise ConnectError(Code.INTERNAL, f"the plugin's {method} raised {type(e).__name__}") from e
        if not isinstance(answer, Answer):
            raise ConnectError(Code.INTERNAL, f"the plugin's {method} returned {type(answer).__name__}, not an Answer")
        data = answer.data
        if call == "Describe":
            if not isinstance(data, dict):
                raise ConnectError(Code.INTERNAL, "the plugin's describe data is not an object")
            data = dict(data, protocol=PACKAGE, contracts=[PACKAGE])
        try:
            response = response_class.from_json(json.dumps(
                {"ok": True, "data": data, "notRead": _not_read(answer.not_read), "warnings": list(answer.warnings)},
                allow_nan=False))
        except (TypeError, ValueError) as e:
            raise ConnectError(Code.INTERNAL, f"the plugin's {method} answer is not JSON: {str(e)[:120]}") from None
        size = len(codec.envelope_json(response))
        if size > cap:
            raise error("resource_exhausted", [f"the answer is larger than outputBytes ({size} > {cap} bytes)"], cap=cap)
        return response


def application(plugin: Any) -> PluginServiceWSGIApplication:
    """The WSGI application serving plugin: Connect JSON only, uncompressed, so sizes are what is sent."""
    return PluginServiceWSGIApplication(_Service(plugin), read_max_bytes=MAX_REQUEST_BYTES, compressions=[],
                                        codecs=[codec.StrictJSONCodec()])


class _Server(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind looks up the host's name (socket.getfqdn), which can stall on a resolver;
        # the server only ever binds to 127.0.0.1, so its name is that.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]
        self.setup_environ()


class _Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def serve(plugin: Any, port: int = 0) -> None:
    """Serves plugin on 127.0.0.1 until interrupted; prints its base URL as the first line of stdout."""
    server = _Server(("127.0.0.1", port), _Quiet)
    server.set_app(application(plugin))
    print(f"http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    sys.stdout.flush()
