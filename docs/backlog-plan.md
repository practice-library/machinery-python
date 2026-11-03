# machinery-python backlog plan (2026-11-02)

Status: the founder signed an earlier draft of this plan on 2026-11-02. This revision answers a read-only review and is resubmitted with the HLD and LLD. The actions are carried out one PR at a time.

Read at `main` 233800c. There are 3 open issues and 0 open PRs. The merged PRs are #3, #4, #6, #8, #10, #12, #14 and #16.

| # | Title (short) | Action | Reason |
|---|---|---|---|
| 1 | Implement the machinery contract in python (waits for contract v0) | **Close**, after filing new issues 1 and 2 below | It is delivered and its premises are out of date. #4 shipped the runtime for contract **v1alpha1** (Connect JSON), not v0 over stdin and stdout. It is not "standard library only": `requirements.txt` pins 8 packages by hash, `machinery-gen` among them. It is not a zipapp. CODEOWNERS, the gate caller and the README landed in #3 and #4. The founder's D5 amends the scope to networked jobs with hash-pinned dependencies. Two things it promised remain open, and each becomes its own issue. |
| 17 | End-to-end test of the example plugin, and a deadline test while the plugin thread runs on | **Rewrite**, then do first | **Part 1 is partly covered.** CI's `conformance` job serves `examples/minimal.py` and runs machinery's runner, `connect_run.py`, at a pinned commit, but against this repository's own 56 fixtures, not machinery's. It is also outside the unit suite, so no guard row can make it fail. Rewrite part 1 as "run the runtime fixtures from the unit suite, so guard rows reach them". It should use machinery's fixtures once the move in new issue 5 lands. **Part 2 is a real gap.** The current deadline test checks only the 504 and its timing, not that the plugin's thread is still running afterwards. Under D4 the runtime must answer on time and may abandon the plugin. The test should pin exactly that. |
| 2 | Add discover and schema-check with the first check.json | **Keep, parked** (or merge with machinery-go#3 into one tracking issue in practice-library/.github) | It is correct, but it waits on an event nobody has planned: no `check.json` is proposed for this repository. machinery-go#3 is the same follow-up. |

## New issues (to file when #1 closes)

1. **First real plugin: fact-refresh on this runtime, through a v1alpha1 adapter.** This is #1's leftover. Under machinery's D7 the job stays in machinery until a v1alpha1 gateway runs it. The adapter follows machinery's stdio binding (D1).
2. **Release: a runtime release that names its contract tag.** This is #1's other leftover. The scheme, the manifest, who may release, and qualification are in the LLD's "Release" section.
3. **The stdio binding (D1).** An entry point beside the loopback listener, speaking machinery's frame. It is passed by the same runtime fixtures over both bindings.
4. **Native wheels for linux arm64.** `protobuf-py-ext` and `pyqwest` have hashes for linux x86_64 and macOS arm64 only. A CI leg on `ubuntu-24.04-arm`, which machinery's proposed rule for runtimes requires, cannot install today.
5. **Consume machinery's runtime fixtures by contract tag.** This repository's 56 runtime fixtures at 233800c move to machinery's `conformance/runtime/` in `contract/v1alpha1.1`, and machinery owns them from then on. This repository pins that tag plus its asset sha256 (machinery LLD §6.6, not a bare commit), and deletes its local copies in the same PR.

## Order

#17 (rewritten) → close #1 and file new issues 1 to 5 → 3 (after machinery's stdio binding) → 5 (after `contract/v1alpha1.1`) → 2 (release) → 1 → 4. #2 stays parked until a `check.json` is proposed.
