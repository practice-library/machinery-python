#!/usr/bin/env python3
"""A minimal plugin on the runtime, for conformance: it speaks the fact-refresh job's schemas, so machinery's
Connect runner can check its answers, but it refreshes nothing. Each call shows one outcome:

  Describe  its name, version, job and verbs; the runtime adds protocol and contracts.
  Plan      no work: a plan with no units, decided nothing (maxParallel 1 shows integers leave as 3, not
            3.0); failed_precondition when the graph has nodes; it overruns its caps when timeSeconds is 1.
  RunUnit   unavailable: it never fetches, so it always asks to be tried later.
  Merge     an answer one byte larger than outputBytes, which the runtime refuses (resource_exhausted).
  Run       not a method here: the runtime answers unimplemented.

  python3 examples/minimal.py [--port 0]   # prints its base URL first
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from machinery_runtime import Answer, Refusal, serve  # noqa: E402

SCHEMAS = "https://github.com/practice-library/machinery/contract/v1alpha1/fact-refresh.schema.json#/$defs/"
REPORT = "https://github.com/practice-library/.github/spec/refresh-report/schema.json"


class Minimal:
    def describe(self, caps):
        return Answer({
            "name": "minimal", "version": "0.1.0", "job": "fact-refresh",
            "verbs": [
                {"name": "plan", "shardable": False, "input": SCHEMAS + "planInput", "output": SCHEMAS + "plan"},
                {"name": "run-unit", "shardable": True, "input": SCHEMAS + "unit", "output": SCHEMAS + "partial"},
                {"name": "merge", "shardable": False, "input": SCHEMAS + "mergeInput", "output": REPORT},
            ]})

    def plan(self, input, caps):
        if caps["timeSeconds"] == 1:
            time.sleep(5)
        if input["nodes"]:
            raise Refusal("failed_precondition", [f"this example plans no node, and the graph has {len(input['nodes'])}"])
        return Answer({"graph": input["graph"], "commit": input["commit"], "run": input["run"],
                       "startedAt": input["now"], "maxParallel": 1, "units": [], "decided": []})

    def run_unit(self, input, network, caps):
        raise Refusal("unavailable", ["this example never fetches: try the unit again later"])

    def merge(self, input, caps):
        return Answer({"padding": "x" * caps["outputBytes"]})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=0)
    serve(Minimal(), ap.parse_args(argv).port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
