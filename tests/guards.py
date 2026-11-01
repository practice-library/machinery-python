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

GUARDS = [
    # (what the guard keeps true, file, code, code without the guard)
    ("integral numbers leave as integers", C, "if isinstance(value, float) and value.is_integer() and abs(value) <= MAX_EXACT:", "if False:"),
    ("only numbers exact as doubles become integers", C, "abs(value) <= MAX_EXACT", "True"),
    ("data is always written", C, '    obj.setdefault("data", None)\n', ""),
    ("the decoder is strict JSON", C, "        strict_load(data)\n", ""),
    ("unknown fields are refused", C, "ignore_unknown_fields=False", "ignore_unknown_fields=True"),
    ("NaN and Infinity are refused", C, "parse_constant=_constant", "parse_constant=float"),
    ("a lone surrogate is refused", C, '    json.dumps(value, ensure_ascii=False).encode("utf-8")  # a lone surrogate cannot be UTF-8\n', ""),
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
    ("an error drops its detail to fit outputBytes", V, "        details = []\n        if _body_size", "        pass\n        if _body_size"),
    ("an error drops its message to fit outputBytes", V, "            message = \"\"", "            pass"),
    ("describe names the runtime's package", V, "data = dict(data, protocol=PACKAGE, contracts=[PACKAGE])", "pass"),
    ("network reaches the plugin with its defaults", V, 'args.append(dict({"hosts": []}, **net) if "replay" not in net else', 'args.append(net if True else'),
    ("a Failure detail carries no debug copy", V, "ErrorDetail(AnyMessage.pack(failure))", "ErrorDetail(failure)"),
    ("a plugin that answers no Answer is internal", V, "if not isinstance(answer, Answer):", "if False:"),
    ("answers are never compressed", V, "compressions=[],", ""),
    ("a Refusal has a warning", V, '        if not warnings or not all(isinstance(w, str) and w for w in warnings):', "        if False:"),
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
