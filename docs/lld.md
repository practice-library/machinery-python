# machinery-python: low-level design (2026-11-02)

Status: the founder signed an earlier draft on 2026-11-02. This revision answers a read-only review and is resubmitted. It describes `main` 233800c, and everything not yet built is marked **proposed**. The rulings it follows (D1, D4, D5, D8, D9) are listed in docs/hld.md.

## Layout

| Path | Lines | Role |
|---|---|---|
| `machinery_runtime/__init__.py` | 17 | Public surface: `Answer`, `Refusal`, `serve` |
| `machinery_runtime/server.py` | 197 | The Connect service, the call pipeline, the deadline (`_within`), the output cap, and the threaded `wsgiref` server on 127.0.0.1 |
| `machinery_runtime/codec.py` | 90 | Strict JSON decoding, and integral numbers written as integers up to 2^53 (above 2^53: HLD gap 8) |
| `machinery_runtime/rules.py` | 80 | The rules proto cannot state |
| `machinery_runtime/failure.py` | 75 | The code mapping, the `Failure` detail, and shrinking an error to fit outputBytes |
| `examples/minimal.py` | 61 | The conformance plugin. Each call shows one outcome |
| `conformance/fixtures/*.json` | 1,232 | 56 runtime fixtures: Describe 24, Plan 7, RunUnit 24, Merge 1 |
| `tests/test_runtime.py` | 439 | 40 tests: most over real HTTP against an in-process server, some calling the codec and rules directly |
| `tests/guards.py` | 97 | 45 guard rows. Each removes one check in a scratch copy and expects the suite to fail |

## Contracts and interfaces

- **Wire, today:** `machinery.plugin.v1alpha1.PluginService`, Connect unary JSON, POST `/<service>/<Call>`, HTTP/1.1 on 127.0.0.1. It follows `contract/v1alpha1/` at machinery `d6024652`. This stays as the reference binding.
- **Wire, proposed (D1):** the stdio binding, exactly as machinery's LLD §2.4 frames it:
  - Stdin carries one JSON line `{"path": ..., "timeoutMs": N}`, then the Connect-JSON request body.
  - Stdout carries one line `{"status": <code or "ok">}`, then the response or the Connect error body.
  - The exit status is 0 on ok and 1 on any error.
  - The host kills the process at `timeoutMs` plus a grace period.
- **Plugin interface (Python):** `describe(caps)`, `plan(input, caps)`, `run_unit(input, network, caps)`, `merge(input, caps)` and `run(input, network, caps)`. Each returns an `Answer(data, not_read, warnings)` or raises `Refusal(code, warnings, not_read)`. A missing method is answered `unimplemented`.
- **Values handed to a plugin:** plain JSON, with integral numbers as `int`. `network` arrives with its defaults filled in.
- **Loopback start-up:** `serve()` prints the base URL as the first line of stdout.

## The call pipeline (`_Service._call`)

1. **Transport errors.** connectrpc answers these before the runtime runs, with **no** Failure detail: 404, 415, a body over 16 MiB, a `Content-Encoding`, or a bad `Connect-Timeout-Ms`.
2. **Decoding.** The codec decodes strictly. Anything wrong is `invalid_argument`, with a Failure.
3. **Rules.** `rules.py` checks the contract's rules. A broken rule is `invalid_argument`, with the rule as the warning.
4. **Deadline.** It is `min(caps.timeSeconds, Connect-Timeout-Ms)`. The plugin runs on a daemon thread. Past the deadline the answer is `deadline_exceeded`, and the thread is abandoned, which D4 allows.
5. **Checking the answer.** It must be an `Answer` with strings within the envelope's limits, otherwise `internal`. Describe data must be an object. The runtime stamps `protocol` and `contracts`.
6. **Writing.** The answer is written with integral numbers as integers. An answer over `caps.outputBytes` is `resource_exhausted`.

Every error the runtime itself answers (steps 2 to 6) carries a Failure detail.

## Tests and conformance

- **Unit:** `python3 -m unittest discover -s tests -t . -v`
- **Guards:** `python3 tests/guards.py`. It runs the suite once per row, 45 times.
- **Conformance:** serve `examples/minimal.py`, then run machinery's `conformance/connect_run.py` (at `d6024652`) on this repository's `conformance/fixtures/`, with jsonschema 17.2.1 validating the refresh report.
- **Proposed (#17, rewritten):**
  - (a) Run the runtime fixtures from the unit suite, so a guard row can reach the end-to-end path.
  - (b) A deadline test showing the plugin thread is still running after the 504: a plugin sets an event after the deadline, and the test waits on it. Plus a guard row.
- **Proposed (new issue 5):** the 56 fixtures move to machinery's `conformance/runtime/` in `contract/v1alpha1.1`, and machinery owns them from then on. This repository keeps a vendored, verified copy, as machinery LLD §6.6 sets out:
  - A pin file with four values: `tag`, `commit`, `asset` (`machinery-contract-v1alpha1.N.tar`) and `sha256`.
  - To update it: download the asset and `SHA256SUMS` from the tag's release, check the sha256 against the pin (a mismatch refuses), check that the tag names the pinned commit, unpack only `conformance/` (runtime fixtures and runner), and record the pin.
  - A test compares the vendored files with the pin, and CI's conformance job reads the pin instead of a bare commit.
  - The local `conformance/fixtures/` copy is deleted in the same PR. The same fixtures run over every binding the runtime offers.

## CI (`.github/workflows/checks.yml`)

- `unit` and `conformance` run on ubuntu-24.04.
- On top of them run the organisation's `leak-scan`, `denylist`, `removed-paths`, `release-note` and `schema-loosening`, at .github v2.1.0, pinned by commit.
- `gate` aggregates them and is the only required check.
- The triggers are pull requests and pushes to main. **There is no tag trigger.**

**Proposed:** a `release` workflow on `v*` tags, and a `ubuntu-24.04-arm` leg once new issue 4 lands.

## Release (proposed)

**Versioning and compatibility**
- **The runtime's version** is `vMAJOR.MINOR.PATCH`, starting at `v0.1.0`. It is not the contract's version.
- **While the version is 0.x:**
  - a change to the Python plugin interface, or a removed binding, raises MINOR;
  - anything else raises PATCH.
- **From v1.0.0:** semantic versioning on the Python interface.
- **The wire** follows the contract tag the release names. Moving to a new contract tag is at least a MINOR release. A new contract major version (v1) is a new runtime MAJOR.

**Who may release**
- Only the practice-library bot, through the organisation's app wrapper.
- The delivery model is the organisation's, as machinery's LLD §6 sets out:
  1. A branch `release/<version>` takes the child PRs, each reviewed at its exact head.
  2. One release PR from that branch to `main`, authored by the bot. Its body carries the self-check, the qualification result and the review receipts.
  3. The release PR merges only when its head passes the pre-tag qualification below. After it merges, the bot tags `v<version>` on the merge commit.

**Pre-tag qualification, at the release PR's exact head, on public GitHub-hosted runners only ($0)**
1. **Source checks:** the unit suite and the mutation guards (`tests/guards.py`) on the checkout. The guards mutate copies of the checkout's sources by design, so they test the source, not the wheel.
2. **Build:** the wheel, from a `pyproject.toml` (proposed). The package is `machinery_runtime`, it needs Python 3.11 or later, and its dependencies match `requirements.txt`. Check its contents: only `machinery_runtime/` and its metadata.
3. **Installed-wheel smoke and conformance, isolated from the checkout:**
   - Make a clean venv outside the checkout, and install the built wheel plus `requirements.txt` with `--require-hashes`.
   - Run from an empty temporary directory, with `python -I`, so the checkout cannot be imported.
   - Assert that `machinery_runtime.__file__` lies under that venv's `site-packages`.
   - Serve a copy of `examples/minimal.py` from the temporary directory. Its `sys.path` insert then points at the temporary directory, not the checkout.
   - Run machinery's runtime fixtures from the vendored copy of the named contract tag, after checking it against the pin, over every binding.

**Publication, at the tagged commit**
1. Repeat steps 2 and 3 above at the exact tagged commit.
2. Only if they pass, attach three assets to the GitHub release: the wheel, `manifest.json`, and the release's `requirements.txt`.
   - The release's `requirements.txt` is the repository's `requirements.txt` plus one line for the wheel itself, hash-pinned: `machinery-runtime @ https://github.com/practice-library/machinery-python/releases/download/v<version>/machinery_runtime-<version>-py3-none-any.whl --hash=sha256:<the manifest's wheel sha256>`.
3. If they fail, nothing is attached. The tag stays, and the fix ships as the next PATCH.

**The manifest** (`manifest.json`, a release asset) records:
- the runtime's version, tag and commit;
- the contract pin: tag, commit, asset name and asset sha256 (machinery LLD §6.6);
- the wheel's file name and sha256;
- `requirements.txt`'s sha256;
- the number of runtime fixtures it passed from that asset, per call;
- the bindings it offers (`loopback`, and `stdio` once built);
- the CI run that qualified it.

**A consumer that pins the contract** follows machinery's release, not this one. It replaces its commit pin with machinery's four-value pin (tag, commit, asset, sha256), starting at `contract/v1alpha1.0`, and changes any test that hard-codes the commit to read the pin file. machinery LLD §6.6 owns those steps.

**A host adopting this runtime** (to launch Python plugins)
1. Download `manifest.json`, the wheel and the release's `requirements.txt` from the release.
2. Check the wheel's sha256 against the manifest.
3. Check that the manifest's contract pin (tag and asset sha256) is the one the host pins.
4. Install with `pip install --require-hashes --only-binary :all: -r requirements.txt`, using the release's file. Its last line pins the wheel by URL and sha256, so the runtime itself is hash-checked like every dependency.
5. Launch plugins through the binding the host speaks: `stdio` for a host that may not open TCP.
6. When the host pins a new runtime release, repeat steps 1 to 3. A MINOR change in 0.x is read as possibly breaking.

## What another runtime must match (input to the language study)

The language choice is machinery's, ruled as D9: Go and Python, with no new repository. To pass the same runtime fixtures, a runtime must do the following.
1. Serve the five calls in Connect unary JSON over each binding it offers, answering `unimplemented`, with a Failure, for a missing method.
2. Decode strictly: UTF-8, no duplicate keys, no NaN or Infinity, no lone surrogates, no unknown fields. It must still accept proto field names, integers as strings, and null for an omitted field.
3. Apply the rules proto cannot state, with the same warning wording (the fixtures compare warnings).
4. Write every integral `Struct`/`Value` number in integer form, at any size: the shortest round-trip digits, with no fraction or exponent (`3`, `9007199254740994`, `100000000000000000000`). This is machinery#29, and machinery-go's rule; this runtime's gap above 2^53 is HLD gap 8.
5. Put a Failure detail on every error the runtime itself answers, and shrink it to fit outputBytes in the same order. Transport-level answers carry none.
6. Answer on time: the deadline is the sooner of `timeSeconds` and the call's timeout, and the runtime may abandon the plugin (D4). Measure the output cap on uncompressed bytes, and never compress.

## Cost

- **Dependencies:** 8 hash-pinned requirements, including `machinery-gen` from machinery's release. Two of them are native (`protobuf-py-ext` and `pyqwest`), and `pyqwest` and `opentelemetry-api` are unused when serving.
- **Generated code in this repository:** none.
- **CI time at 233800c:** 162 s in all. `unit` took 150 s, most of it the 45 guard rows each re-running the suite; `conformance` took 12 s. A language comparison should compare conformance time.

## Dry run: a vendor's CLI (paper only, proposed)

What this repository would add for the worked example, after D1's binding exists in machinery:
1. **A new `machinery_runtime/stdio.py`** speaking machinery's frame through the same codec, rules, call and failure pipeline. The deadline is enforced in-process (D4), and the host also ends the process. A guard row would cover it: dropping the status line must fail a test.
2. **The adapter.** It maps fact-refresh's verbs onto `machinery_runtime`, with v0 exit codes mapped to Connect codes: 1 to `failed_precondition`, 2 to `invalid_argument`, 3 to `unimplemented`, 75 to `unavailable`. It lives in machinery while machinery D7 holds.
3. **A replay fixture** shaped like the vendor's CLI: `network: {"hosts": [], "replay": ...}` recording the upstream hosts' answers at commit `1c6ac8c3c4d2c3dec2cb5966d225ca2a0e0471d3` (the source reference is graphs#68). It proves replay passes through the runtime unchanged.
4. **Imports from graphs:** none at runtime. A test may copy one node of the vendor's graph as input, pinned by graphs commit and sha256, once graphs#68 lands.
