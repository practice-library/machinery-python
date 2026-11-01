"""Serves a plugin as machinery.plugin.v1alpha1.PluginService: Connect unary JSON over HTTP/1.1 on loopback.

Per call, the runtime and not the plugin:
  - decodes strictly (codec.py) and checks the rules proto cannot state (rules.py): invalid_argument;
  - answers unimplemented for a call the plugin has no method for;
  - calls the plugin with plain JSON values (integral numbers as integers, as on the way out), and gives up
    after caps.timeSeconds or Connect-Timeout-Ms, whichever is sooner: deadline_exceeded (the plugin's
    thread is left to finish on its own);
  - writes the answer with every envelope field and integral numbers as integers, and refuses one larger
    than caps.outputBytes: resource_exhausted;
  - turns a Refusal into its Connect code, and a plugin bug into internal.
Every one of these errors carries its Failure detail and fits caps.outputBytes (failure.py). What connectrpc
answers before the runtime runs has no detail, as a transport's answer: 404 for a path not served (Connect
clients read it as unimplemented), 415 for a body that is not application/json, and Connect errors for a
request over 16 MiB, a Content-Encoding, or a Connect-Timeout-Ms that is not a number.
"""
from __future__ import annotations

import json
import socketserver
import sys
import threading
from dataclasses import dataclass
from typing import Any, Sequence
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

from machinery.plugin.v1alpha1 import plugin_pb as pb
from machinery.plugin.v1alpha1.plugin_connect import PluginServiceWSGIApplication

from . import codec, failure, rules

PACKAGE = "machinery.plugin.v1alpha1"
MAX_REQUEST_BYTES = 16 << 20
CODES = {k: v for k, v in failure.CODES.items() if k not in ("deadline_exceeded", "internal")}  # a Refusal's
# call: (the plugin's method, whether it gets network, the response message)
CALLS = {"Describe": ("describe", False, pb.DescribeResponse), "Plan": ("plan", False, pb.PlanResponse),
         "RunUnit": ("run_unit", True, pb.RunUnitResponse), "Merge": ("merge", False, pb.MergeResponse),
         "Run": ("run", True, pb.RunResponse)}
error = failure.error


@dataclass(frozen=True)
class Answer:
    """What a call answers when it succeeds. not_read holds (what, why) pairs. Warnings are 1 to 500
    characters and not_read's what and why 1 to 300 (envelope.schema.json); an Answer that breaks that is
    answered as internal."""
    data: Any
    not_read: Sequence[tuple[str, str]] = ()
    warnings: Sequence[str] = ()


class Refusal(Exception):
    """A call that fails: code is one of CODES, with at least one warning saying why, each within
    envelope.schema.json's limits (a Refusal that breaks them raises ValueError, which is internal)."""

    def __init__(self, code: str, warnings: Sequence[str], not_read: Sequence[tuple[str, str]] = ()):
        if code not in CODES:
            raise ValueError(f"a Refusal's code is one of {sorted(CODES)}, not {code!r}")
        if not warnings:
            raise ValueError("a Refusal has at least one warning")
        why = failure.check_strings(warnings, not_read)
        if why:
            raise ValueError(why)
        super().__init__("; ".join(warnings))
        self.code, self.warnings, self.not_read = code, list(warnings), list(not_read)


class _Late(Exception):
    pass


def _within(seconds: float, fn, *args):
    """fn(*args), or _Late after seconds; the plugin's own exceptions pass through."""
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
        raise _Late()
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
        req = codec.integers(json.loads(request.to_json()))  # Struct holds doubles: 5, never 5.0
        caps = req.get("caps") or {}
        cap = caps.get("outputBytes") or None
        why = rules.check(call, req)
        if why:
            raise error("invalid_argument", [why], cap=cap)
        fn = getattr(self.plugin, method, None)
        if fn is None:
            raise error("unimplemented", [f"this plugin does not answer {call}"], cap=cap)
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
        except _Late:
            raise error("deadline_exceeded", [f"the call did not finish within {seconds:g} seconds"], cap=cap) from None
        except Refusal as r:
            raise error(r.code, r.warnings, r.not_read, cap) from None
        except Exception as e:  # noqa: BLE001 # the plugin's own bug: internal, naming only its type
            raise error("internal", [f"the plugin's {method} raised {type(e).__name__}"], cap=cap) from e
        if not isinstance(answer, Answer):
            raise error("internal", [f"the plugin's {method} returned {type(answer).__name__}, not an Answer"], cap=cap)
        why = failure.check_strings(answer.warnings, answer.not_read)
        if why:
            raise error("internal", [f"the plugin's {method} answer: {why}"], cap=cap)
        data = answer.data
        if call == "Describe":
            if not isinstance(data, dict):
                raise error("internal", ["the plugin's describe data is not an object"], cap=cap)
            data = dict(data, protocol=PACKAGE, contracts=[PACKAGE])
        try:
            response = response_class.from_json(json.dumps(
                {"ok": True, "data": data, "notRead": failure.not_read_json(answer.not_read), "warnings": list(answer.warnings)},
                allow_nan=False))
        except (TypeError, ValueError) as e:
            raise error("internal", [f"the plugin's {method} answer is not JSON: {str(e)[:120]}"], cap=cap) from None
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
