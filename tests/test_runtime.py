"""The runtime: the codec, the request rules, and the server end to end on 127.0.0.1. Black-box conformance
against machinery's fixtures runs in CI (conformance/fixtures); these tests cover what the fixtures cannot
reach or name."""
from __future__ import annotations

import base64
import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from machinery.plugin.v1alpha1 import plugin_pb as pb

from machinery_runtime import Answer, Refusal, codec, rules, server

SVC = "/machinery.plugin.v1alpha1.PluginService/"
CAPS = {"timeSeconds": 60, "outputBytes": 1048576}


class Codec(unittest.TestCase):
    def test_strict_json_refuses_a_key_twice_nan_infinity_a_lone_surrogate_and_bad_utf8(self):
        for body in (b'{"a": 1, "a": 2}', b'{"a": NaN}', b'{"a": -Infinity}', b'{"a": "\\ud800"}', b'\xff{}'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                codec.strict_load(body)
        self.assertEqual(codec.strict_load(b'{"a": {"b": [1, 2.5]}}'), {"a": {"b": [1, 2.5]}})

    def test_integral_numbers_up_to_2_53_become_integers(self):
        self.assertEqual(codec.integers({"a": [3.0, 2.5, -0.0, 2.0 ** 53, 2.0 ** 53 + 2, True]}),
                         {"a": [3, 2.5, 0, 2 ** 53, 2.0 ** 53 + 2, True]})
        self.assertIsInstance(codec.integers(2.0 ** 53 + 2), float)

    def test_the_envelope_has_every_field_and_integers(self):
        msg = pb.PlanResponse.from_json('{"ok": true, "data": {"n": 3, "x": 0.5}}')
        self.assertEqual(json.loads(codec.envelope_json(msg)),
                         {"ok": True, "data": {"n": 3, "x": 0.5}, "notRead": [], "warnings": []})
        self.assertIn(b'"n":3,', codec.envelope_json(msg))
        self.assertEqual(json.loads(codec.envelope_json(pb.PlanResponse())),
                         {"ok": False, "data": None, "notRead": [], "warnings": []})

    def test_a_message_that_is_not_an_envelope_is_refused(self):
        with self.assertRaises(ValueError):
            codec.envelope_json(pb.Caps(time_seconds=1))

    def test_decode_refuses_unknown_fields_and_keeps_protos_leniency(self):
        c = codec.StrictJSONCodec()
        for body in (b'{"caps": {"timeSeconds": 1, "timeSeconds": 2}}', b'{"caps": {"timeSeconds": NaN}}'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                c.decode(body, pb.DescribeRequest)
        with self.assertRaises(Exception):
            c.decode(b'{"caps": {"timeSeconds": 1, "outputBytes": 1024, "retries": 1}}', pb.DescribeRequest)
        req = c.decode(b'{"caps": {"time_seconds": "60", "output_bytes": 2048}}', pb.DescribeRequest)
        self.assertEqual((req.caps.time_seconds, req.caps.output_bytes), (60, 2048))


def frame(**over):
    return dict({"caps": dict(CAPS)}, **over)


class Rules(unittest.TestCase):
    def test_caps_bounds(self):
        for caps, ok in (({"timeSeconds": 1, "outputBytes": 1024}, True), ({"timeSeconds": 3600, "outputBytes": 16777216}, True),
                         ({"timeSeconds": 0, "outputBytes": 1024}, False), ({"timeSeconds": 3601, "outputBytes": 1024}, False),
                         ({"timeSeconds": 1, "outputBytes": 1023}, False), ({"timeSeconds": 1, "outputBytes": 16777217}, False),
                         ({"outputBytes": 1024}, False), ({"timeSeconds": 1}, False)):
            with self.subTest(caps=caps):
                self.assertEqual(rules.check("Describe", {"caps": caps}) is None, ok)
        self.assertEqual(rules.check("Describe", {}), "caps is required")

    def test_input_on_every_call_but_describe_and_network_on_rununit_and_run(self):
        self.assertIsNone(rules.check("Describe", frame()))
        for call in ("Plan", "Merge", "RunUnit", "Run"):
            self.assertEqual(rules.check(call, frame()), "input is required")
        for call in ("RunUnit", "Run"):
            self.assertEqual(rules.check(call, frame(input={})), f"network is required on {call}")
            self.assertIsNone(rules.check(call, frame(input={}, network={})))
        self.assertIsNone(rules.check("Plan", frame(input={})))

    def test_hosts(self):
        for host, ok in (("docs.example.com", True), ("a-b.c0", True), ("Docs.example.com", False), ("localhost", False),
                         ("-a.com", False), ("a-.com", False), ("a..com", False), ("docs.example.com\n", False),
                         ("dоcs.example.com", False), ("docs.example.com.", False)):
            with self.subTest(host=host):
                self.assertEqual(rules.network({"hosts": [host]}) is None, ok)

    def test_replay_goes_with_an_empty_hosts_and_a_start(self):
        start = "2026-10-31T12:00:00Z"
        self.assertIsNone(rules.network({"hosts": [], "replay": {"start": start}}))
        self.assertIn("both hosts and replay", rules.network({"hosts": ["a.com"], "replay": {"start": start}}))
        for bad in ("", "2026-10-31T12:00:00", "2026-10-31T12:00:00.5Z", "2026-10-31 12:00:00Z", start + "\n"):
            with self.subTest(start=bad):
                self.assertIn("replay.start", rules.network({"replay": {"start": bad}}))

    def recorded(self, rec):
        return rules.network({"replay": {"start": "2026-10-31T12:00:00Z", "exchanges": {"https://a.com/": [rec]}}})

    def test_a_recorded_response(self):
        for rec, ok in (({"status": 200.0}, True), ({"status": 100}, True), ({"status": 599}, True),
                        ({"status": 200, "headers": {"a": "b"}, "body": "x"}, True), ({"error": "reset"}, True),
                        ({"status": 99}, False), ({"status": 600}, False), ({"status": 200.5}, False),
                        ({"status": True}, False), ({"status": "200"}, False), ({}, False), ({"body": "x"}, False),
                        ({"error": ""}, False), ({"error": 1}, False), ({"error": "x", "status": 200}, False),
                        ({"error": "x", "body": ""}, False), ({"status": 200, "headers": {"a": 1}}, False),
                        ({"status": 200, "headers": []}, False), ({"status": 200, "body": 1}, False),
                        ({"status": 200, "delay": 1}, False), (200, False), ([], False)):
            with self.subTest(rec=rec):
                self.assertEqual(self.recorded(rec) is None, ok, self.recorded(rec))


class Plugin:
    """Answers or fails as the test sets it; records what it was called with."""

    def __init__(self):
        self.calls, self.answer = [], None

    def describe(self, caps):
        self.calls.append(("describe", caps))
        return Answer({"name": "t", "version": "1.0.0", "job": "j", "verbs": [], "protocol": "wrong"})

    def plan(self, input, caps):
        self.calls.append(("plan", input, caps))
        return self.answer(input, caps) if callable(self.answer) else self.answer

    def run_unit(self, input, network, caps):
        self.calls.append(("run_unit", input, network, caps))
        return Answer({})


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = Plugin()
        cls.server = server._Server(("127.0.0.1", 0), server._Quiet)
        cls.server.set_app(server.application(cls.plugin))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.plugin.calls.clear()

    def post(self, call, body, headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(self.url + SVC + call, data=data, method="POST",
                                     headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers.get("Content-Type"), e.read()

    def plan(self, answer, **caps):
        self.plugin.answer = answer
        return self.post("Plan", {"caps": dict(CAPS, **caps), "input": {"k": 1}})

    def test_describe_gets_the_runtimes_package_whatever_the_plugin_says(self):
        status, ctype, body = self.post("Describe", {"caps": CAPS})
        self.assertEqual((status, ctype), (200, "application/json"))
        data = json.loads(body)["data"]
        self.assertEqual((data["protocol"], data["contracts"]), (server.PACKAGE, [server.PACKAGE]))

    def test_the_plugin_gets_plain_json_and_caps(self):
        status, _, body = self.plan(Answer({"n": 2}, not_read=[("a", "b")], warnings=["w"]))
        self.assertEqual(json.loads(body), {"ok": True, "data": {"n": 2}, "notRead": [{"what": "a", "why": "b"}], "warnings": ["w"]})
        self.assertEqual(self.plugin.calls, [("plan", {"k": 1}, CAPS)])

    def test_network_reaches_the_plugin_with_its_defaults_filled_in(self):
        unit = {"caps": CAPS, "input": {}}
        for net, seen in (({}, {"hosts": []}), ({"hosts": ["a.com"]}, {"hosts": ["a.com"]}),
                          ({"replay": {"start": "2026-10-31T12:00:00Z"}},
                           {"hosts": [], "replay": {"start": "2026-10-31T12:00:00Z", "exchanges": {}}})):
            with self.subTest(net=net):
                self.plugin.calls.clear()
                self.assertEqual(self.post("RunUnit", dict(unit, network=net))[0], 200)
                self.assertEqual(self.plugin.calls[0][2], seen)

    def test_a_refusal_carries_its_envelope_as_a_failure_detail(self):
        status, _, body = self.plan(lambda i, c: (_ for _ in ()).throw(
            Refusal("unavailable", ["later", "really"], not_read=[("x", "y")])))
        err = json.loads(body)
        self.assertEqual((status, err["code"], err["message"]), (503, "unavailable", "later; really"))
        [detail] = err["details"]
        self.assertEqual(detail["type"], "machinery.plugin.v1alpha1.Failure")
        self.assertNotIn("debug", detail)
        value = detail["value"]
        failure = pb.Failure.from_binary(base64.b64decode(value + "=" * (-len(value) % 4)))
        self.assertEqual(json.loads(failure.to_json()), {"data": None, "notRead": [{"what": "x", "why": "y"}],
                                                         "warnings": ["later", "really"]})

    def test_an_error_body_fits_output_bytes_dropping_the_detail_then_the_message(self):
        long = ["w" * 300]
        for cap, has_detail, has_message in ((1024, True, True), (400, False, True), (40, False, False)):
            err = server.error("failed_precondition", long, cap=cap)
            size = server._body_size("failed_precondition", err.message, list(err.details))
            with self.subTest(cap=cap):
                self.assertEqual((bool(err.details), bool(err.message)), (has_detail, has_message))
                if has_message:
                    self.assertLessEqual(size, cap)

    def test_the_body_size_is_what_is_sent(self):
        self.plugin.answer = lambda i, c: (_ for _ in ()).throw(Refusal("failed_precondition", ["no " * 20]))
        _, _, body = self.post("Plan", {"caps": CAPS, "input": {}})
        err = server.error("failed_precondition", ["no " * 20], cap=CAPS["outputBytes"])
        self.assertEqual(len(body), server._body_size("failed_precondition", err.message, list(err.details)))

    def test_an_answer_over_output_bytes_is_resource_exhausted(self):
        status, _, body = self.plan(Answer({"s": "x" * 2000}), outputBytes=1024)
        self.assertEqual((status, json.loads(body)["code"]), (429, "resource_exhausted"))
        self.assertLessEqual(len(body), 1024)
        status, _, body = self.plan(Answer({"s": "x" * 900}), outputBytes=1024)
        self.assertEqual(status, 200)
        self.assertLessEqual(len(body), 1024)

    def test_the_deadline_is_the_sooner_of_time_seconds_and_connect_timeout_ms(self):
        self.plugin.answer = lambda i, c: (time.sleep(3), Answer({}))[1]
        started = time.monotonic()
        status, _, body = self.post("Plan", {"caps": dict(CAPS, timeSeconds=1), "input": {}})
        self.assertEqual((status, json.loads(body)["code"]), (504, "deadline_exceeded"))
        self.assertLess(time.monotonic() - started, 2.5)
        started = time.monotonic()
        status, _, body = self.post("Plan", {"caps": CAPS, "input": {}}, {"Connect-Timeout-Ms": "300"})
        self.assertEqual(json.loads(body)["code"], "deadline_exceeded")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_a_plugin_that_breaks_is_internal_never_a_success(self):
        for answer in ({"not": "an Answer"}, Answer(float("nan")), Answer(object()),
                       lambda i, c: 1 / 0):
            with self.subTest(answer=answer):
                status, _, body = self.plan(answer)
                self.assertEqual((status, json.loads(body)["code"]), (500, "internal"))
                self.assertNotIn("details", json.loads(body))

    def test_a_call_without_a_method_is_unimplemented(self):
        status, _, body = self.post("Merge", {"caps": CAPS, "input": {}})
        self.assertEqual((status, json.loads(body)["code"]), (501, "unimplemented"))

    def test_only_json_is_served(self):
        self.assertEqual(self.post("Describe", b"\x0a\x00", {"Content-Type": "application/proto"})[0], 415)
        status, _, _ = self.post("Describe", {"caps": CAPS}, {"Content-Encoding": "gzip"})
        self.assertNotEqual(status, 200)

    def test_answers_are_never_compressed_so_their_size_is_what_is_counted(self):
        req = urllib.request.Request(self.url + SVC + "Describe", data=json.dumps({"caps": CAPS}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertIn(r.headers.get("Content-Encoding"), (None, "identity"))
            self.assertTrue(json.loads(r.read())["ok"])

    def test_a_rule_broken_is_invalid_argument_with_the_rule(self):
        status, _, body = self.post("Describe", {"caps": dict(CAPS, outputBytes=1023)})
        err = json.loads(body)
        self.assertEqual((status, err["code"]), (400, "invalid_argument"))
        self.assertTrue(err["message"].startswith("caps.outputBytes must be 1024 to 16777216"))


class RefusalShape(unittest.TestCase):
    def test_a_refusal_needs_a_known_code_and_a_warning(self):
        for args in (("internal", ["x"]), ("unavailable", []), ("unavailable", [""]), ("unavailable", [1])):
            with self.subTest(args=args), self.assertRaises(ValueError):
                Refusal(*args)


if __name__ == "__main__":
    unittest.main()
