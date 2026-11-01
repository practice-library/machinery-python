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

from connectrpc.code import Code
from connectrpc.errors import ConnectError
from machinery.plugin.v1alpha1 import plugin_pb as pb

from machinery_runtime import Answer, Refusal, codec, failure, rules, server

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
        self.assertIs(type(codec.integers(2.0 ** 53)), int)
        self.assertIs(type(codec.integers(-(2.0 ** 53))), int)

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
            with self.subTest(body=body), self.assertRaises(ConnectError) as raised:
                c.decode(body, pb.DescribeRequest)
            self.assertEqual((raised.exception.code, len(raised.exception.details)), (Code.INVALID_ARGUMENT, 1))
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
        self.assertEqual(status, 503)
        err = json.loads(body)
        self.assertEqual((err["code"], err["message"]), ("unavailable", "later; really"))
        [detail] = err["details"]
        self.assertEqual(detail["type"], "machinery.plugin.v1alpha1.Failure")
        self.assertNotIn("debug", detail)
        value = detail["value"]
        failure = pb.Failure.from_binary(base64.b64decode(value + "=" * (-len(value) % 4)))
        self.assertEqual(json.loads(failure.to_json()), {"data": None, "notRead": [{"what": "x", "why": "y"}],
                                                         "warnings": ["later", "really"]})

    # the HTTP status Connect answers for each code the runtime uses: a test checks it before it decodes the Failure,
    # so a runtime that answers a success, or another error, fails on the status and not with KeyError (#15)
    STATUS = {"invalid_argument": 400, "internal": 500, "unimplemented": 501, "deadline_exceeded": 504}

    def code_of(self, answered, status):
        """The Connect code of an answer, after its HTTP status is checked, so a runtime that answers a success or
        another error fails on the status and not with KeyError (#15)."""
        got, _, body = answered
        self.assertEqual(got, status)
        return json.loads(body)["code"]

    def failure_of(self, body):
        err = json.loads(body)
        self.assertIn("details", err, "the error has no Failure detail")
        [detail] = err["details"]
        value = detail["value"]
        env = json.loads(pb.Failure.from_binary(base64.b64decode(value + "=" * (-len(value) % 4))).to_json())
        self.assertEqual(err["message"], "; ".join(env["warnings"]))
        return err, env

    def test_an_error_fits_output_bytes_by_saying_less_never_by_dropping_its_detail(self):
        # at the smallest outputBytes the contract allows, with warnings it allows (up to 500 characters)
        for warnings, not_read in ((["w" * 420], ()), (["é" * 300], ()), (["a" * 500, "b" * 500], [("x" * 300, "y" * 300)])):
            err = failure.error("unavailable", warnings, not_read, cap=1024)
            with self.subTest(first=warnings[0][:3]):
                self.assertEqual(len(err.details), 1)
                self.assertTrue(warnings[0].startswith(err.message) and err.message)
                self.assertLessEqual(failure.body_size("unavailable", err.message, err.details), 1024)

    def test_an_error_says_less_in_order_not_read_then_later_warnings_then_the_first_ones_end(self):
        full = failure.error("unavailable", ["one", "two"], [("qq", "zz")])
        size = failure.body_size("unavailable", full.message, full.details)
        self.assertEqual(failure.error("unavailable", ["one", "two"], [("qq", "zz")], cap=size).details[0].message_bytes,
                         full.details[0].message_bytes)
        less = failure.error("unavailable", ["one", "two"], [("qq", "zz")], cap=size - 1)
        self.assertEqual((less.message, b"qq" in less.details[0].message_bytes), ("one; two", False))
        both = failure.body_size("unavailable", less.message, less.details)
        self.assertEqual(failure.error("unavailable", ["one", "two"], cap=both - 1).message, "one")
        one = failure.error("unavailable", ["one"])
        self.assertEqual(failure.error("unavailable", ["one"], cap=failure.body_size("unavailable", one.message, one.details) - 1).message, "on")

    def test_only_below_the_contracts_smallest_output_bytes_does_an_error_lose_its_detail(self):
        err = failure.error("invalid_argument", ["caps.outputBytes must be 1024 to 16777216, not 40"], cap=40)
        self.assertEqual((err.message, list(err.details)), ("", []))

    def test_a_body_that_does_not_decode_gets_an_error_that_fits_any_legal_output_bytes(self):
        # caps.outputBytes is inside the body that did not decode, so the error fits the smallest legal one
        for key in ("k" * 2000, "\U0001F600" * 600):
            body = json.dumps({"caps": {"timeSeconds": 60, "outputBytes": 1024}, key: 1}).encode()
            with self.subTest(key=key[:3]):
                status, _, raw = self.post("Describe", body)
                self.assertEqual(status, 400)
                self.assertLessEqual(len(raw), 1024)
                err, _ = self.failure_of(raw)
                self.assertEqual(err["code"], "invalid_argument")

    def test_the_first_warnings_prefix_fits_exactly_at_the_cap(self):
        warning = "x" * 600
        for k in (1, 37, 499):
            whole = failure.error("internal", [warning[:k]])
            cap = failure.body_size("internal", whole.message, whole.details)
            with self.subTest(k=k):
                self.assertEqual(failure.error("internal", [warning], cap=cap).message, warning[:k])
                self.assertEqual(failure.error("internal", [warning], cap=cap - 1).message, warning[:k - 1])

    def test_a_warning_longer_than_the_envelope_allows_is_cut_to_500_characters(self):
        err = failure.error("internal", ["w" * 600])
        self.assertEqual(err.message, "w" * 500)

    def test_a_number_that_overflows_to_infinity_is_invalid_argument_with_its_failure(self):
        big = "1" + "0" * 400  # a plain integer literal: Python reads it as an int, protobuf-py as infinity
        for input_json in ('{"a": -1e400}', '{"a": [1, 1e400]}', '{"a": {"b": 1E999}}', '{"a": %s}' % big,
                           '{"a": [-%s]}' % big):
            body = ('{"caps": {"timeSeconds": 60, "outputBytes": 1048576}, "input": %s}' % input_json).encode()
            with self.subTest(input=input_json):
                status, _, raw = self.post("Plan", body)
                self.assertEqual(status, 400)  # first, so a runtime that lets it through fails on the status
                self.assertEqual(self.failure_of(raw)[0]["code"], "invalid_argument")

    # 2**1024 - 2**970 is the midpoint between the largest double and 2**1024: below it a literal rounds to the
    # largest double, which is finite; from it on it rounds to infinity (#9)
    EDGE = 2 ** 1024 - 2 ** 970

    def plan_with(self, n):
        return self.post("Plan", b'{"caps": {"timeSeconds": 60, "outputBytes": 1048576}, "input": {"a": %d}}' % n)

    def test_an_integer_that_rounds_to_a_finite_double_is_accepted(self):
        self.assertEqual(codec.strict_load(b'{"a": %d}' % (2 ** 1023)), {"a": 2 ** 1023})
        self.plugin.answer = Answer({})
        for n in (int(1.7976931348623157e308), self.EDGE - 1, -(self.EDGE - 1)):
            with self.subTest(n=n):
                self.assertEqual(self.plan_with(n)[0], 200)

    def test_an_integer_that_rounds_to_infinity_is_invalid_argument_with_its_failure(self):
        self.plugin.answer = Answer({})
        for n in (self.EDGE, -self.EDGE):
            with self.subTest(n=n):
                status, _, raw = self.plan_with(n)
                self.assertEqual(status, 400)  # first, so a runtime that lets it through fails on the status
                self.assertEqual(self.failure_of(raw)[0]["code"], "invalid_argument")

    def test_a_decoding_error_is_fitted_to_exactly_the_smallest_legal_output_bytes(self):
        from unittest import mock
        with mock.patch.object(failure, "error", wraps=failure.error) as built, self.assertRaises(ConnectError):
            codec.StrictJSONCodec().decode(b'{"caps": {"retries": 1}}', pb.DescribeRequest)
        self.assertEqual(built.call_args.kwargs["cap"], 1024)

    def test_a_refusal_whose_not_read_is_not_a_pair_is_internal_with_its_failure(self):
        status, _, raw = self.plan(lambda i, c: (_ for _ in ()).throw(Refusal("unavailable", ["w"], not_read=[("x",)])))
        self.assertEqual(status, 500)
        self.assertEqual(self.failure_of(raw)[0]["code"], "internal")

    def test_every_error_the_runtime_answers_carries_a_failure(self):
        self.plugin.answer = lambda i, c: (time.sleep(2), Answer({}))[1]
        cases = [("Merge", {"caps": CAPS, "input": {}}, {}, "unimplemented"),
                 ("Plan", {"caps": dict(CAPS, timeSeconds=1), "input": {}}, {}, "deadline_exceeded"),
                 ("Describe", {"caps": dict(CAPS, outputBytes=1023)}, {}, "invalid_argument"),
                 ("Describe", b'{"caps": {"timeSeconds": 1, "timeSeconds": 2}}', {}, "invalid_argument"),
                 ("Describe", b'{"caps": {"retries": 1}}', {}, "invalid_argument"),
                 ("Describe", b"", {}, "invalid_argument")]
        for call, body, headers, code in cases:
            with self.subTest(call=call, body=body):
                status, _, raw = self.post(call, body, headers)
                self.assertEqual(status, self.STATUS[code])
                err, env = self.failure_of(raw)
                self.assertEqual((err["code"], env.get("data", None)), (code, None))
        for answer in ({"not": "an Answer"}, lambda i, c: 1 / 0):
            with self.subTest(answer=answer):
                status, _, raw = self.plan(answer)
                self.assertEqual(status, 500)
                self.assertEqual(self.failure_of(raw)[0]["code"], "internal")

    def test_the_plugin_gets_integral_numbers_as_integers(self):
        self.plugin.answer = Answer({})
        self.post("Plan", {"caps": CAPS, "input": {"n": 5, "l": [1, 2.5], "big": 2 ** 53 + 2}})
        got = self.plugin.calls[0][1]
        self.assertEqual([type(got["n"]), type(got["l"][0]), type(got["l"][1])], [int, int, float])
        self.plugin.calls.clear()
        self.post("RunUnit", {"caps": CAPS, "input": {}, "network": {"replay": {
            "start": "2026-10-31T12:00:00Z", "exchanges": {"https://a.com/": [{"status": 200}]}}}})
        self.assertIs(type(self.plugin.calls[0][2]["replay"]["exchanges"]["https://a.com/"][0]["status"]), int)

    def test_an_answer_or_refusal_outside_the_envelopes_limits_is_internal(self):
        for answer in (Answer({}, warnings=[""]), Answer({}, warnings=["w" * 501]), Answer({}, not_read=[("", "y")]),
                       Answer({}, not_read=[("x", "y" * 301)]), Answer({}, not_read=[("x",)]),
                       lambda i, c: (_ for _ in ()).throw(Refusal("unavailable", ["w" * 501])),
                       lambda i, c: (_ for _ in ()).throw(Refusal("unavailable", ["w"], not_read=[("x",)]))):
            with self.subTest(answer=answer):
                self.assertEqual(self.code_of(self.plan(answer), 500), "internal")
        self.assertEqual(self.plan(Answer({}, warnings=["w" * 500], not_read=[("x" * 300, "y" * 300)]))[0], 200)

    def test_describe_data_that_is_not_an_object_is_internal(self):
        saved = Plugin.describe
        Plugin.describe = lambda self, caps: Answer(["not", "an", "object"])
        try:
            self.assertEqual(self.code_of(self.post("Describe", {"caps": CAPS}), 500), "internal")
        finally:
            Plugin.describe = saved

    def test_an_answer_of_exactly_output_bytes_passes_and_one_byte_more_fails(self):
        msg = pb.PlanResponse.from_json(json.dumps({"ok": True, "data": {"s": "x" * 1500}}))
        size = len(codec.envelope_json(msg))
        self.assertEqual(self.plan(Answer({"s": "x" * 1500}), outputBytes=size)[0], 200)
        self.assertEqual(self.plan(Answer({"s": "x" * 1500}), outputBytes=size - 1)[0], 429)

    def test_the_body_size_is_what_is_sent(self):
        self.plugin.answer = lambda i, c: (_ for _ in ()).throw(Refusal("failed_precondition", ["no " * 20]))
        _, _, body = self.post("Plan", {"caps": CAPS, "input": {}})
        err = failure.error("failed_precondition", ["no " * 20], cap=CAPS["outputBytes"])
        self.assertEqual(len(body), failure.body_size("failed_precondition", err.message, err.details))

    def test_an_answer_over_output_bytes_is_resource_exhausted(self):
        answered = self.plan(Answer({"s": "x" * 2000}), outputBytes=1024)
        self.assertEqual(self.code_of(answered, 429), "resource_exhausted")
        self.assertLessEqual(len(answered[2]), 1024)
        status, _, body = self.plan(Answer({"s": "x" * 900}), outputBytes=1024)
        self.assertEqual(status, 200)
        self.assertLessEqual(len(body), 1024)

    def test_the_deadline_is_the_sooner_of_time_seconds_and_connect_timeout_ms(self):
        self.plugin.answer = lambda i, c: (time.sleep(3), Answer({}))[1]
        started = time.monotonic()
        self.assertEqual(self.code_of(self.post("Plan", {"caps": dict(CAPS, timeSeconds=1), "input": {}}), 504),
                         "deadline_exceeded")
        self.assertLess(time.monotonic() - started, 2.5)
        started = time.monotonic()
        self.assertEqual(self.code_of(self.post("Plan", {"caps": CAPS, "input": {}}, {"Connect-Timeout-Ms": "300"}), 504),
                         "deadline_exceeded")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_a_plugin_that_breaks_is_internal_never_a_success(self):
        for answer in ({"not": "an Answer"}, Answer(float("nan")), Answer(object()),
                       lambda i, c: 1 / 0):
            with self.subTest(answer=answer):
                self.assertEqual(self.code_of(self.plan(answer), 500), "internal")

    def test_a_call_without_a_method_is_unimplemented(self):
        self.assertEqual(self.code_of(self.post("Merge", {"caps": CAPS, "input": {}}), 501), "unimplemented")

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
        self.assertEqual(status, 400)
        err = json.loads(body)
        self.assertEqual(err["code"], "invalid_argument")
        self.assertTrue(err["message"].startswith("caps.outputBytes must be 1024 to 16777216"))


class Bind(unittest.TestCase):
    def test_the_server_binds_without_looking_up_a_host_name(self):
        import socket
        from unittest import mock
        with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("looked up")):
            srv = server._Server(("127.0.0.1", 0), server._Quiet)
        self.assertEqual(srv.server_name, "127.0.0.1")
        srv.server_close()


class RefusalShape(unittest.TestCase):
    def test_a_refusal_needs_a_known_code_and_a_warning(self):
        for args in (("internal", ["x"]), ("unavailable", []), ("unavailable", [""]), ("unavailable", [1])):
            with self.subTest(args=args), self.assertRaises(ValueError):
                Refusal(*args)


if __name__ == "__main__":
    unittest.main()
