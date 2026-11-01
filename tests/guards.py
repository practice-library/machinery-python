"""Each guard, removed, must make the runtime's tests fail.

For every row: copy the package and the tests to a temporary directory, replace the guard's code (which must
occur exactly once, so a row cannot silently stop applying), run tests.test_runtime and expect it to fail.
Run: python3 tests/guards.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R, C, V = "machinery_runtime/rules.py", "machinery_runtime/codec.py", "machinery_runtime/server.py"
F = "machinery_runtime/failure.py"

GUARDS = [
    # (what the guard keeps true, file, code, code without the guard)
    ("integral numbers leave as integers", C, "if isinstance(value, float) and value.is_integer() and abs(value) <= MAX_EXACT:", "if False:"),
    ("only numbers exact as doubles become integers", C, "abs(value) <= MAX_EXACT", "True"),
    ("data is always written", C, '    obj.setdefault("data", None)\n', ""),
    ("the decoder is strict JSON", C, "        strict_load(data)\n", ""),
    ("unknown fields are refused", C, "ignore_unknown_fields=False", "ignore_unknown_fields=True"),
    ("an integer too large for a double is refused", C, "object_pairs_hook=_pairs, parse_int=_integer)", "object_pairs_hook=_pairs)"),
    ("an integer that rounds to a finite double is accepted", C, "        float(value)\n",
     "        float(value)\n        if abs(value) > int(1.7976931348623157e308):\n            raise OverflowError\n"),
    ("a lone surrogate or a non-finite number is refused", C, '    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")\n', ""),
    ("a number that overflows to infinity is refused", C, "ensure_ascii=False, allow_nan=False)", "ensure_ascii=False)"),
    ("caps bounds hold", R, "if not low <= value <= high:", "if not low - 1 <= value <= high + 1:"),
    ("input is required but on Describe", R, 'if call != "Describe" and "input" not in request:', "if False:"),
    ("network is required on RunUnit and Run", R, '            return f"network is required on {call}"\n', "            return None\n"),
    ("a host is matched whole", R, "if not HOST.fullmatch(host):", "if not HOST.match(host):"),
    ("a listed host with replay is refused", R, "    if hosts:\n        return \"network has both", "    if False:\n        return \"network has both"),
    ("the replay start is matched whole", R, "if not START.fullmatch(start):", "if not START.match(start):"),
    ("a recorded error stands alone", R, "        if len(rec) != 1:", "        if False:"),
    ("a recorded status is an integer", R, "and float(status).is_integer()", ""),
    ("a recorded response has no unknown field", R, "    extra = sorted(set(rec) - RECORDED_KEYS)", "    extra = []"),
    ("recorded headers are strings", R, "all(isinstance(v, str) for v in headers.values())", "True"),
    ("Connect-Timeout-Ms shortens the deadline", V, "seconds = min(seconds, max(left, 0) / 1000)", "pass"),
    ("an answer over outputBytes is refused", V, "        if size > cap:", "        if False:"),
    ("describe names the runtime's package", V, "data = dict(data, protocol=PACKAGE, contracts=[PACKAGE])", "pass"),
    ("network reaches the plugin with its defaults", V, 'args.append(dict({"hosts": []}, **net) if "replay" not in net else', 'args.append(net if True else'),
    ("a Failure detail carries no debug copy", F, "ErrorDetail(AnyMessage.pack(pb.Failure.from_json(json.dumps(envelope))))", "ErrorDetail(pb.Failure.from_json(json.dumps(envelope)))"),
    ("a plugin that answers no Answer is internal", V, "if not isinstance(answer, Answer):", "if False:"),
    ("answers are never compressed", V, "compressions=[],", ""),
    ("the plugin gets integral numbers as integers", V, "req = codec.integers(json.loads(request.to_json()))", "req = json.loads(request.to_json())"),
    ("an error drops notRead first to fit", F, "        message, details = _parts(warnings, ())\n", "        pass\n"),
    ("an error keeps its detail by shortening its first warning", F, "if body_size(code, *_parts([first[:mid]], ())) <= cap:", "if False:"),
    ("an error fits at exactly outputBytes", F, "    if cap is not None and body_size(code, message, details) > cap:\n        message, details = _parts(warnings, ())", "    if cap is not None and body_size(code, message, details) >= cap:\n        message, details = _parts(warnings, ())"),
    ("a warning within the envelope's limits", F, "not 1 <= len(w) <= WARNING_MAX", "not 1 <= len(w)"),
    ("a not_read entry within the envelope's limits", F, "1 <= len(s) <= NOT_READ_MAX for s in pair", "True for s in pair"),
    ("an Answer outside the envelope's limits is internal", V, "        why = failure.check_strings(answer.warnings, answer.not_read)\n        if why:", "        why = None\n        if why:"),
    ("a decoding error carries a Failure", C, 'raise failure.error("invalid_argument", [f"the body is not a valid', 'raise ValueError(*[f"the body is not a valid'),
    ("unimplemented carries a Failure", V, 'raise error("unimplemented", [f"this plugin does not answer {call}"], cap=cap)', 'raise ConnectError(Code.UNIMPLEMENTED, "no")'),
    ("deadline_exceeded carries a Failure", V, 'raise error("deadline_exceeded", [f"the call did not finish within {seconds:g} seconds"], cap=cap) from None', 'raise ConnectError(Code.DEADLINE_EXCEEDED, "late") from None'),
    ("describe data that is not an object is internal", V, "            if not isinstance(data, dict):", "            if False:"),
    ("an answer of exactly outputBytes passes", V, "        if size > cap:", "        if size >= cap:"),
    ("the server binds without looking up a host name", V, "        socketserver.TCPServer.server_bind(self)\n        self.server_name", "        WSGIServer.server_bind(self)\n        self.server_name"),
    ("a decoding error fits the smallest outputBytes", C, "cap=rules.OUTPUT_BYTES[0]) from e", "cap=None) from e"),
    ("the first warning's prefix fits exactly at the cap", F, "if body_size(code, *_parts([first[:mid]], ())) <= cap:", "if body_size(code, *_parts([first[:mid]], ())) < cap:"),
    ("a warning is cut to the envelope's 500 characters", F, "    warnings = [w[:WARNING_MAX] for w in warnings]\n", ""),
    ("a not_read entry is a pair", F, "isinstance(pair, (tuple, list)) and len(pair) == 2", "isinstance(pair, (tuple, list))"),
    ("a decoding error is fitted to exactly the smallest legal outputBytes", C, "cap=rules.OUTPUT_BYTES[0]) from e", "cap=rules.OUTPUT_BYTES[0] + 1) from e"),
    ("a Refusal has a warning", V, "        if not warnings:\n            raise", "        if False:\n            raise"),
]


def main() -> int:
    failed = []
    for what, path, code, mutant in GUARDS:
        with tempfile.TemporaryDirectory() as tmp:
            for d in ("machinery_runtime", "tests"):
                shutil.copytree(os.path.join(ROOT, d), os.path.join(tmp, d), ignore=shutil.ignore_patterns("__pycache__"))
            target = os.path.join(tmp, path)
            with open(target, encoding="utf-8") as f:
                text = f.read()
            if text.count(code) != 1:
                failed.append(f"{what}: the guard's code occurs {text.count(code)} times in {path}")
                continue
            with open(target, "w", encoding="utf-8") as f:
                f.write(text.replace(code, mutant))
            out = subprocess.run([sys.executable, "-m", "unittest", "tests.test_runtime"], cwd=tmp,
                                 capture_output=True, text=True)
            if out.returncode == 0:
                failed.append(f"{what}: tests.test_runtime still passes without the guard")
            else:
                print(f"ok   {what}")
    for f in failed:
        print(f"FAIL {f}")
    print(f"{len(GUARDS) - len(failed)} of {len(GUARDS)} guards are tested")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
