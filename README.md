# machinery-python
Python implementation of the machinery plugin contract: the thin runtime that serves a plugin as `machinery.plugin.v1alpha1.PluginService`.

It implements contract v1alpha1 of [practice-library/machinery](https://github.com/practice-library/machinery) (plan practice-library/machinery#17, PR 4): Connect unary JSON over HTTP/1.1 on 127.0.0.1, the calls `Describe`, `Plan`, `RunUnit`, `Merge` and `Run`, and the rules of `contract/v1alpha1/` at machinery commit `d6024652`. v1alpha1 may still change. From v1 on, a version changes only additively, and anything else is a new major version. Contract v0 (stdin and stdout) is not implemented here.

## The runtime

A plugin is an object with some of these methods. The runtime answers `unimplemented` for the ones it lacks:

```python
from machinery_runtime import Answer, Refusal, serve

class Plugin:
    def describe(self, caps): ...                   # Answer({"name", "version", "job", "verbs"})
    def plan(self, input, caps): ...                # Answer(data, not_read=[(what, why)], warnings=[...])
    def run_unit(self, input, network, caps): ...
    def merge(self, input, caps): ...
    def run(self, input, network, caps): ...        # raise Refusal(code, warnings) to fail

serve(Plugin())  # prints http://127.0.0.1:<port> first, then serves until interrupted
```

The plugin receives plain JSON values, with integral numbers as integers (`5`, not the `5.0` a `Struct` holds):
- `input` is the call's input.
- `caps` is `{"timeSeconds", "outputBytes"}`.
- `network` is `{"hosts": [...]}`, or `{"hosts": [], "replay": {"start", "exchanges"}}`, with the defaults filled in.

The plugin itself does only its job. The runtime does the rest:

- **Decoding** (`machinery_runtime/codec.py`). The body must be strict JSON: UTF-8, no key given twice, no NaN or Infinity, no lone surrogate. Every field must be known. Proto's JSON leniency is kept: proto field names, integers written as strings, and null for a field that is left out. Anything else is `invalid_argument`.
- **The rules proto cannot state** (`machinery_runtime/rules.py`). These are the caps bounds, input on every call but `Describe`, network on `RunUnit` and `Run`, the host pattern, replay only with an empty hosts, the replay start, and each recorded response. A broken rule is `invalid_argument`, with the rule as the warning.
- **Answers before the runtime runs.** connectrpc answers these as a transport would, with no Failure detail: 404 for a path that is not served (Connect clients read it as `unimplemented`), 415 for a body that is not `application/json`, and Connect errors for a request over 16 MiB, a `Content-Encoding`, or a `Connect-Timeout-Ms` that is not a number.
- **The deadline.** It is `caps.timeSeconds` or `Connect-Timeout-Ms`, whichever is sooner. After it the answer is `deadline_exceeded`; the plugin's thread is left to finish on its own.
- **Writing the answer.** Every envelope field is written. A number whose value is integral is written as an integer: `3`, never `3.0` (machinery#29, since protobuf-py writes doubles with a fraction). An answer larger than `caps.outputBytes` is refused as `resource_exhausted`.
- **Errors** (`machinery_runtime/failure.py`). A `Refusal` becomes its Connect code. A plugin that raises anything else, or returns something other than a valid `Answer`, gets `internal`, never a success. Every error the runtime answers, including its own `invalid_argument`, `unimplemented`, `deadline_exceeded` and `internal`, has its warnings joined by `"; "` as the message and its envelope (`ok` false, `data` null) as a `machinery.plugin.v1alpha1.Failure` detail, as the plugin.proto header requires. An error body fits `caps.outputBytes` by saying less, never by dropping the detail: notRead goes first, then every warning but the first, then the end of that warning. Warnings are 1 to 500 characters and not_read's what and why 1 to 300, as `envelope.schema.json` allows; a `Refusal` or `Answer` that breaks this is `internal`.
- **What it serves.** Only Connect JSON, uncompressed, so the bytes counted against `outputBytes` are the bytes sent. The server is the standard library's `wsgiref`, threaded, on loopback only; gRPC is not served.

## Dependencies

`requirements.txt` pins every dependency by hash. They are:
- the generated code, `machinery-gen` 0.1.0, from machinery's release `gen/go/v0.1.0`, rebuildable to the same sha256;
- the Connect runtime, `connectrpc` 0.13.0;
- its message library, `protobuf-py` 0.7.0;
- their dependencies.

The native wheels have hashes for linux x86_64 and macOS arm64 only. Python 3.11 or later is required.

## Checks

`.github/workflows/checks.yml` runs these jobs:
- `unit` runs the runtime's tests and `tests/guards.py`. Each guard row removes one check from the runtime, and its test must then fail.
- `conformance` serves `examples/minimal.py` and runs machinery's Connect runner (`conformance/connect_run.py` at the pinned commit) on `conformance/fixtures/`, with schema validation. That example plugin speaks the fact-refresh job's schemas but refreshes nothing; each of its calls shows one outcome.
- The organisation's reusable workflows from practice-library/.github at release v2.1.0, pinned by full commit: `leak-scan` and the four review guards (`denylist`, `removed-paths`, `release-note`, `schema-loosening`).
- The `gate` that the ruleset requires (`gate / gate`).

`.github/required-checks.json` lists exactly the gate's needs. `.github/denylist.json` is the organisation's list, copied from practice-library/.github at that release. Every path is owned by the maintainers (`.github/CODEOWNERS`). `.github/workflows/automerge.yml` merges only allowlisted paths (`.github/automerge-paths.json`, which allows none yet).
