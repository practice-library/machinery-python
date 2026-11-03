# machinery-python: high-level design (2026-11-02)

Status: the founder signed an earlier draft on 2026-11-02. This revision answers a read-only review and is resubmitted. It describes `main` 233800c, and everything not yet built is marked **proposed**.

Rulings made at signing, which this document follows:
- **D1:** the transport is stdio, and loopback HTTP stays as the reference binding.
- **D4:** a runtime must answer on time and may abandon a late plugin; the host ends the process.
- **D5:** Python is a machinery runtime for networked jobs, with hash-pinned dependencies.
- **D8 (machinery):** the contract is released under its own tag, `contract/v1alpha1.N`. The planned first tag is `contract/v1alpha1.0` at machinery `65b3d2ce34c67c7887a2564b3f694239aebb642d` (asset `machinery-contract-v1alpha1.0.tar`, sha256 `f9c513a94e735ec850f1ba09d8a13f919342b34c2e4aad1c8eaa4a774cffad4f`).
- **D9 (machinery):** the runtime languages are Go and Python, with no new repository.

## Purpose

machinery-python is the **Python runtime for the machinery plugin contract**. A plugin author writes an object with up to five methods (`describe`, `plan`, `run_unit`, `merge`, `run`). The runtime serves it as `machinery.plugin.v1alpha1.PluginService` and keeps the contract's promises so the plugin doesn't have to: strict decoding, the rules proto cannot state, the deadline, the output cap, integral numbers, and a Failure detail on the errors the runtime itself answers.

It exists because of two machinery decisions. machinery#6 made machinery a contract with one implementation per language, each in its own repository. machinery#17 moved the calls to a `.proto` service.

## Boundaries

| Owns | Does not own |
|---|---|
| The Python runtime package `machinery_runtime` | The contract: `proto/`, `contract/v1alpha1/`, the bindings' framing, and the conformance runner (practice-library/machinery) |
| One example plugin, `examples/minimal.py`, that refreshes nothing | Real plugins. fact-refresh stays in machinery until a v1alpha1 gateway runs it (machinery D7) |
| **Until `contract/v1alpha1.1`:** a copy of the 56 runtime fixtures in `conformance/fixtures/`. From `.1` on this repository **consumes** them from machinery by contract tag plus asset sha256 and owns none (backlog new issue 5) | The runtime fixtures: machinery owns them in `conformance/runtime/` from `contract/v1alpha1.1`, moved from this repository's 56 at 233800c. Also the job fixtures in `conformance/connect/` |
| The proof that it conforms: unit tests, guard rows, and the conformance job | The generated code. `machinery-gen` is built and released by machinery |
| Its own releases (proposed; see "Release") | The contract release (`contract/v1alpha1.N`, machinery D8) |
| Its CI wiring to the organisation's gate | The gateway that schedules plan, run-unit and merge (practice-library/.github, machinery D3) |
| | Contract v0 (stdin and stdout, exit codes), which is deliberately not implemented here |

## Consumers

- **Plugin authors in Python.** Today the only plugin is the example.
- **Hosts that call a served plugin.** That means machinery's conformance runner, a v1alpha1 gateway (once machinery D3 is done), and any caller that speaks the contract. A caller limited to the standard library, with no TCP, reaches this runtime only through the stdio binding (D1).
- **Other runtimes.** This runtime's fixtures and behaviour are the reference for what a new runtime must match (LLD, "What another runtime must match"). The language choice itself is machinery's: `docs/languages.md` there, ruled as D9.

## Release (proposed)

There are two releases, and they must not be confused:
1. **The contract release** is machinery's: `contract/v1alpha1.N` (D8), an archive of `proto/`, `contract/` and `conformance/` with its sha256 (machinery LLD §6.4). The runtime fixtures are in it from `.1`. **A consumer that pins the contract pins this tag and its asset sha256, not a runtime** (machinery LLD §6.6).
2. **The runtime release** is this repository's: `vMAJOR.MINOR.PATCH`, starting at `v0.1.0`. Each release **names the one contract tag, and that tag's asset sha256, it was qualified against**, and records that in a manifest attached to the release. `v0.1.0` must qualify against `contract/v1alpha1.1` or later, after that tag is published; `.1` is the first tag that carries the runtime fixtures. No contract tag is published yet.

Each release is cut by the practice-library bot only, on public GitHub-hosted runners at no cost. The release PR's head must pass the pre-tag qualification before it merges and is tagged; all of the pre-tag checks (source suite and mutation guards, build, isolated installed-wheel smoke and conformance) then run again at the tagged commit before any asset is published. The LLD gives the steps, the manifest's fields, the compatibility policy, and a recipe for a host adopting this runtime.

## Known gaps

1. **The timeout truth.** After the deadline the runtime answers `deadline_exceeded`, but the plugin's thread runs on. D4 allows this. Under stdio the process ends with the call, so the thread dies with it. Under loopback the host must supervise. It is tested only for the 504 and its timing (#17).
2. **Integral numbers.** protobuf-py writes `3.0` (machinery#29). `codec.integers` works around it at 233800c, and guard rows hold the workaround. machinery#29 stays open until machinery has read the fix and its `3.0` fixtures pass against it.
3. **No stdio binding yet** (D1). The runtime listens on 127.0.0.1 only.
4. **Platforms.** The native wheels cover linux x86_64 and macOS arm64, but not linux arm64.
5. **No release, and no tag trigger in CI.**
6. **Fixture pin lag.** CI runs machinery's runner at the bare commit `d6024652`. The family pins three different machinery commits today. D8 resolves this: this repository moves to a contract tag plus asset sha256.
7. **No real plugin yet.**
8. **Integral numbers above 2^53.** The contract writes every integral number in integer form, at any size. This runtime does so only up to 2^53, so `2^53 + 2` leaves as `9007199254740994.0` and `1e20` as `1e+20`. machinery-go writes the shortest round-trip digits in integer form (`9007199254740994`, `100000000000000000000`). A fix PR aligns this runtime with machinery-go.

## Issues studied

- **machinery:** #6, #17, #29, #31 (item 3, the namespace clash, is resolved because the package is `machinery_runtime`), #42 to #49 (fact-refresh). Also machinery's draft HLD (D1 to D9, §6.1), LLD (§2.4 bindings, §3 conformance) and `docs/languages.md`.
- **machinery-go:** #1 and #3, the Go counterparts of this repository's #1 and #2.
- **graphs:** #68 (the worked example's graph, open), #91 (its denylist exemption, open). Remote `main` has no language or runtime nodes among its six vendor graphs.
- **Private repositories:** the consumer's bridge and plug-contract issues, and the network and operations trackers, were read. No open network issue names machinery. The findings that matter here are stated generically above. The private references live in the private record.

## Dry run: a vendor's CLI (paper only)

This walks one change through the chain in the founder's addendum. The worked example is a vendor's CLI whose graph holds 126 nodes sourced at the upstream release commit `1c6ac8c3c4d2c3dec2cb5966d225ca2a0e0471d3`. The upstream repository, its release tag and the 28 source URLs are named in graphs#68 (public). The repository's name is on the organisation's denylist, so it is not repeated here. A new upstream release changes a fact, such as a worktree state or a destructive-flag marker.

| Step | Who | What machinery-python does | What it imports from graphs | What is missing |
|---|---|---|---|---|
| 1. Facts | graphs, the vendor's graph (graphs#68; its denylist exemption is graphs#91) | Nothing. It never reads graph nodes | Nothing. The runtime is job-agnostic | graphs#91, then #68 |
| 2. Refresh | machinery's fact-refresh (#42, #47, #49) | Nothing. machinery owns the job and its fixtures | — | The job is a contract v0 plugin today (`factrefresh/plugin.py`), and the shipped gateway runs v0 (machinery D3). #49 must be fixed before #68 merges, or other graphs lose their hosts |
| 3. Serve | **machinery-python** | **Proposed:** it serves fact-refresh through a v1alpha1 adapter over the stdio binding. It enforces the deadline and the output cap, and the host ends the process after each call (D4). `network.hosts` names the two upstream hosts. Tests use replay | Nothing | The stdio binding (new issue 3), the adapter (new issue 1), a release (new issue 2), and a v1alpha1 gateway (machinery D3) |
| 4. Guard input | A consumer CLI limited to the standard library | **Proposed:** nothing directly. The consumer reads graphs at a pinned ref. If it calls a plugin, it runs this runtime's stdio entry point per call | — | Its bridge, which waits on D1's binding and D8's tag |
| 5. Records | The founder's private record | Nothing. PR bodies and release PRs carry the receipts it links | — | — |

### Transport (ruled: D1 = stdio)

The founder chose between the two options machinery's HLD §6.1 set out. The comparison is kept as the record of why.

| | **(A) stdio**, one process per call: **chosen** | **(B) unix socket**: not chosen |
|---|---|---|
| Shape | machinery's frame: one JSON line naming the path and `timeoutMs`, then the Connect-JSON body. On stdout, one status line, then the body | The same HTTP as loopback, on a socket file |
| Change here | A stdio entry point beside the listener, running the same codec, rules and failure pipeline | A unix listener |
| A late plugin (D4) | It ends with the process | The host must supervise a long-lived process |
| The standard-library consumer | No exception needed | It would need a rule exception for a socket per plugin. That was never granted |
| Cost | Python's imports run on every call (about tens of calls per refresh run; not yet measured) | A socket lifecycle per job |
