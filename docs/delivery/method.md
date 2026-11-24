# Delivery method

How Disyard epics are delivered one at a time.
Every epic goes through four steps: prepare, build, verify, close.
Prepare produces one file, the epic breakdown spec (`docs/delivery/epics/NN-<slug>.md`).
The captain reviews that file once, and that review is the alignment step for the domain.
Lane issues are filed on GitHub only after approval, and the other lanes start only after lane 0 is merged.

## Alignment rule

Before executing a domain, its breakdown spec is reviewed once by the captain.
Approval is the alignment, and no separate design meeting is held.
During execution, work relies on the architecture note, the pinned contracts and the epic.
When a domain closes, the note and the epic are refined with what was learned.

## Artifacts, one source of truth each

| Thing | Lives in | Form |
| --- | --- | --- |
| What a domain must deliver | the epic issue (label `epic`, GitHub Project 2) | GitHub issue |
| How to deliver it in parallel | `docs/delivery/epics/NN-<slug>.md` | repo file, reviewed as a PR |
| Why the system has this shape | `docs/architecture/` (draw.io files, one note per area as each domain executes) | repo files |
| Pinned contracts | `contracts/<name>/` | schema files, tables, test vectors, a loader test, `README.md` |
| How a release is cut | [`docs/delivery/release-process.md`](release-process.md) | repo file |
| Spike results | `docs/delivery/spikes/S<n>/<slug>.md`, throwaway code in `docs/delivery/spikes/S<n>/code/` | repo files |
| Work units | GitHub issues labelled `lane:<x>` and `epic:<n>`, sub-issues of the epic | issue from the lane issue template |

Filed as GitHub issues: epics, lane issues (after approval) and spikes (label `spike`, timeboxed).
Kept as files: breakdown specs, contracts, spike results and architecture notes.
A breakdown spec is not a design spec, so it does not use the `spec-template` skill.
That skill stays for design decisions inside an architecture note.
Epic text and notes link to contracts and never copy them.

Templates:

- [Epic breakdown spec](templates/epic-breakdown.md)
- [Lane issue](../../.github/ISSUE_TEMPLATE/lane-issue.md)
- [Pull request](../../.github/pull_request_template.md)

The lane issue template is a markdown template, not an issue form, because workers file with `gh-axi issue create --body-file` and issue forms apply only in the web UI.

## One epic, step by step

### Prepare

1. The author reads the epic, its neighbours' Interfaces tables, `contracts/` and the architecture note.
2. The author writes `docs/delivery/epics/NN-<slug>.md` from the template.
   The questions the captain must answer are the first section, at most five.
3. Before review, the read-only spec agents run and their findings are fixed: `spec-coherence`, `spec-citation-verifier`, `spec-ticket-coverage`, `spec-adversary`.
4. The captain reviews the spec PR once.
   Approve, comment, or answer the listed questions.
5. On merge, the lane issues are filed from the spec's issue blocks, the spec keeps only `#NNN` links, spikes are filed, and an epic-wide tracking comment is opened on the epic issue.

### Build

1. Lane 0 merges first, alone.
   It carries the pinned contracts, stubs for every cross-lane dependency and the justfile module for the domain.
   Lane 0 is the only barrier, and every other lane runs in parallel after it.
2. Each worker uses its own worktree and branch `epic-NN/<lane>/<issue>`, with one PR per issue, squash merged to `main`.
3. Spikes that gate lanes run before those lanes are filed.
4. A cross-lane change is a PR that touches `contracts/` only, labelled `contract-change`, with a comment listing the consumer issues.
   Consumers rebase.

### Verify

1. Per issue, the worker runs the tests and commands the issue names and pastes their output in the PR.
2. Per epic, a worker who did not write the lanes runs the exit test, the epic's stated checklist, on a clean export of `main` and records the run in the spec's Closure section.

### Close

1. The definition of done below is fully ticked.
2. The architecture note is refined with what was learned.
   The epic issue gets a closing comment covering what was built, what moved and what was deferred to which epic, and is closed.
3. The next epic's breakdown inputs are updated: contract versions and changed estimates.

## Lane isolation and merging

1. One owner per path.
   The spec's lane table gives each lane exclusive globs.
   Stubs created in lane 0 belong to lane 0 until merged, then to the lane that replaces them.
2. Shared files are made disjoint by construction.
   - `justfile` only `import`s `just/<domain>.just`, and a domain edits its own module.
   - The `flake.nix` shell hook only sources `scripts/env.sh`, owned by the Python lane.
   - Each Python package is `packages/<name>/pyproject.toml`, owned by one lane.
   - `uv.lock` is the one unavoidable hotspot: a lock conflict is never resolved by hand, `uv lock` is rerun after rebase, and a dependency lands in the PR that needs it.
   - `deploy/versions.yaml` is the second hotspot: one key per line, and a PR touches only its own keys.
   - `Tiltfile` loads one `deploy/<component>/Tiltfile.module` per owner.
3. Every stub has a contract test that runs against the stub and against the real thing, so a stub cannot drift silently.
   Lane 0 writes the stub and the test, and the owning lane makes the real one pass it.
4. `main` stays green (the CI `check` job).
   There is no long-lived integration branch, and a lane that falls more than a day behind rebases.
   Within a lane, issues merge in order when the issue says `needs`, and across lanes any order works.
5. Containers use a worker slot: project name `dsy-<slot>`, host ports `127.0.0.1:<base+slot*10+k>`, label `dsy.dev/verify-run=<run id>`.
   The kind cluster is single-owner: every recipe that touches it takes `flock /tmp/dsy-cluster.lock`, and a recipe refuses to drive a cluster it did not start.
6. An out-of-lane edit is a review rejection, not a merge conflict to resolve.
   The worker comments on the issue and the spec author fixes the lane table.

## Contracts and spikes

Contracts are pinned in lane 0 of the epic that produces them, merged before any issue that consumes them is filed.
A pinned contract has a schema or table file, test vectors, a loader test that fails if a vector breaks, a `version` and an owner epic.
Changing it after the pin is a contract-change PR with a consumer list and a version bump.
Adding a contract, or changing its owner or consumers, also updates its row in `docs/contracts.md` and the diagram `docs/architecture/15-contracts.drawio` in the same PR, and `scripts/check-contracts-map.sh` enforces the row.

Spikes are timeboxed, each one issue labelled `spike` with a binary pass line, throwaway code in `docs/delivery/spikes/S<n>/code/`, and a result file `docs/delivery/spikes/S<n>/<slug>.md` stating pass or fail, the numbers and the decision it settles.
A spike belongs to the epic whose design its answer can change, and the lanes it gates are not filed until the result is merged.
Spikes that need a runtime but change no product file are the only work allowed alongside the one active epic.

## Definition of done per epic

1. Every lane issue is closed with acceptance criteria ticked and evidence linked, as pasted output per the PR evidence section below.
2. The exit test (spec section 7) passed from a clean export of `main`, run by a worker who did not write the lanes.
   The command output is kept in the PR and summarised in Closure.
3. CI is green on `main`, and when the epic added a CI job, one green run on `main` is linked in Closure.
4. Contracts produced are pinned at a version, and every consumer epic's spec links the version.
5. The architecture note is refined, and diagrams are re-exported if touched (`scripts/export-diagrams.sh --check` clean).
6. The epic issue carries a closing comment and is closed.
7. No `TODO` without an issue, and no stub left that a later epic does not own by name.

## PR evidence

A pull request body follows the [template](../../.github/pull_request_template.md): Summary, Notable decisions, Test plan and Risks.
Every Test plan line is a checked box naming the exact command run, followed by a collapsed block holding the real output, response body or log lines, trimmed to the relevant part.
Evidence is output that was captured, never a description of it, and a UI or rendered output carries a screenshot or rendered text.
A criterion that cannot run yet says which later work it waits for.
The local gate line names `nix develop -c just pre-pr` and pastes its `== unit tests:` and `== integration tests:` lines with the pytest summaries, or names `nix develop -c just ci --checks` and the `just ci --shard <name>` runs when `just pre-pr` asked for the full run.
The green CI `check` run on the pull request is the full-gate evidence, linked by its run url.

## Comments

Code and config comments explain intent and never name an issue, epic or task, because those references rot.
Ownership and sequencing live in the issue, the epic spec and the PR.
A stub's runtime message and exit code are a recipe contract and may name an owner, but its comment does not.

## Revisiting a closed epic

A closed epic stays closed and its text is not rewritten.
When later work in another domain exposes a gap in an earlier, merged epic, the needed change becomes a child issue of that earlier epic (a GitHub sub-issue), labelled `revisit`.
The child issue:

- links the later epic or lane that exposed the gap
- lists the consumer epics affected
- names the check or test it touches
- blocks the later lane until it is merged

A change to a pinned contract is not a revisit issue by itself.
It is a contract-change PR with a consumer list and a version bump, as in the contracts section above.

The earlier epic's breakdown spec gets a dated Closure addendum line linking the child issue.
The later epic's breakdown spec lists the child issue under Consumes.

## The verifier convention

Retired on 2026-11-15 with the `verify` and `verify-clean` recipes (https://github.com/AndoNorth/disyard/issues/366), because no epic built a verifier.
Each issue names its tests or commands, and each epic's exit test is a command checklist, run by a worker who did not write the lanes.
Before reporting done, a worker runs `nix develop -c just pre-pr`, `just doctor` and every test or command the issue names, and reports the commit sha, each result, and what was not run and why.
`just pre-pr` runs lint and the other checks of `just ci`, the unit tests of the changed packages and of the packages that depend on them, and the integration tests of the changed packages.
When it prints `full run required`, the change reaches tests no package owns, and the worker runs `nix develop -c just ci --checks`, plus `just ci --shard <name>` for each shard that owns tests the change can reach (`scripts/ci-shards.sh` lists them), instead of the whole serial `just ci`.
The pull request's CI run is the full proof, and the merge gate does not change.
`just verify-lint`, part of `just ci` and of `just pre-pr`, still checks any `verify-<domain>` skill that exists, and passes when none does.

## Epic order

Order follows the needs-first arrows between epics, with one risk-first adjustment: the three runtime spikes (S3, S4, S5) run right after #2, because a failed spike changes the design of epics that would otherwise already be built.
An epic is opened only when everything it consumes is closed or stubbed in lane 0.
Where owners and consumers form a cycle (#10 with #11, #3 with #12), a lane-0 stub breaks it.

| Pos | Epic | Contracts pinned in its lane 0 | Spikes |
| --- | --- | --- | --- |
| E1 | #1 Dev environment | `versions.yaml` schema, `just` recipe table | none |
| E2 | #2 Cluster and Tilt | preset schema, module contract, pool label and taint, namespaces | S2 footprint (#2-13), gate G1 |
| E2.5 | spike block | none | S3 Temporal with Python (owner #6), S4 AMQP 1.0 client (#7), S5 object store (#3-8) |
| E3 | #4 Envelope | tranche A: events and keys, status, artifact reference and bucket layout, instrumentation contract (#13-1) | none |
| E4 | #5 Validator | job document and plan JSON Schema, limits profile | none |
| E5 | #3 State and storage | accepted-jobs row DDL, event hypertable, store contract | S5 result applied |
| E6 | #12 Artifacts | aggregate index format, failure taxonomy | none |
| E7 | #11 Toolchain, guest contract, workloads, aggregate | WIT and guest contract, `build-info.json`, cache entry | S1b heavy compile (#11-15), S6 cold-start storm (#11-9) |
| E8 | #6 Workflow engine | activity contract, failure class table, Temporal naming, static pool table v0 | S3 result applied |
| E9 | #9 Placement | pool table (real values), timeout formulas | none |
| E10 | #7 Broker | tenant, class and cap config format, DLQ reason taxonomy | S4 result applied |
| E11 | #10 Workers | none new | none |
| E12 | #8 Admission | none new | none |
| E13 | #24 Dispatch and control API | API routes, error body, reason codes, status response | S7 100k submit (optional lane) |
| Gate | #17 pass 1: smoke | smoke recipe | none |
| E15 | #13 Observability | catalogue (already pinned in E3) | none |
| E16 | #17 pass 2: e2e | acceptance fixtures | none |
| E17 | #18 Checkpoint and resume | checkpoint contract v1 | none |
| E18 | #15 Failure scenarios | none | none |
| E19 | #14 Load CLI | scenario schema | none |
| E20 | #16 Characterization and soak | baseline report format | none |

The E numbers are positions, not epic numbers: E14 is the smoke gate and E2.5 is a block.
The gate is `just smoke`, one level 0 job from #24 through #8, #6, #9, #10 and #12 to a stored result, with ten consecutive passes before the next epic opens.

Where each epic sits, in one line each:

- #2 settles host fit, the one finding that can stop the PoC on this machine, and ships the module contract and a dummy module, while each real infra module is delivered by its owner epic.
- The spike block is three independent spikes, one worker each, writing only under `docs/delivery/spikes/`.
- #4 is a pure library needing only #1, and its contracts are consumed by almost everything after it.
- #7 can sit anywhere before #8, and sits at E10 because the S4 result is known by then and the host is free.
- #11 defers `paced_sim` and kill-and-resume (issues 5 and 12) to #18's pass, and S6 feeds #10 budgets.
- #18 is not ready until the base is proven, so it follows the smoke gate and e2e.
- #16 is last by design: it needs the #13 series, #14, #15, a green smoke and the host decision.

### Where spikes and contract items go

| Item | Goes to | When |
| --- | --- | --- |
| S1b heavy compile | #11 issue 15 | E7, first lane |
| S2 footprint | #2 issue 13 | E2, exit |
| S3 Temporal with Python | #6 issue 1 plus a new spike issue | spike block, result applied in E8 |
| S4 AMQP 1.0 client | #7 issue 3 | spike block, result applied in E10 |
| S5 object store (settles D10) | #3 issue 8 | spike block, result applied in E5 |
| S6 cold-start storm | #11 issue 9 | E7, first lane, feeds #10 and a possible #2 preset edit |
| S7 100,000-row submit | #24 optional lane | E13 |
| Contract 1 Events and keys | #4 lane 0 | E3 |
| Contract 2 Status (D12 decided in the E3 review) | #4 lane 0 | E3 |
| Contract 3 Artifacts (reference, buckets, `build-info.json`; the aggregate index format is written by #12) | #4 lane 0, index in #12 lane 0 | E3, E6 |
| Contract 4 Activity (payloads, failure class table, heartbeat details) | #6 lane 0 | E8 |
| Contract 5 Pools and Temporal naming | #6 lane 0 (static v0), #9 lane 0 (real) | E8, E9 |
| Contract 6 API and config (route table, error body; tenant and class config) | config in #7 lane 0, API in #24 lane 0 | E10, E13 |
| Contract 7 WIT | #11 lane 0 | E7 |
| Instrumentation contract (#13 issues 1, 2) | #4 lane 0, pulled forward | E3 |

Contract 3's artifact reference is needed by #4 (result ready event) before #12 runs, so it is pinned in E3.
That is the only place a contract is pinned before its owner epic.

### Allowed overlaps

Exactly three, each with a rule that keeps it small:

1. #1 and #2 share one issue: the `kind smoke` CI job moves to #2's exit and is that epic's acceptance, and #1 closes with the lint, test and flake-check jobs.
2. #17 is delivered in two passes.
   Pass 1 (smoke) is the gate and pass 2 (e2e) follows #13.
   The ladder document and pytest harness skeleton were planned as lane 0 of #1 and were not delivered; pass 2 of #17 owns them ([smoke gate spec](epics/17-acceptance-smoke.md#1-goal-and-non-goals)).
3. The spike block and pulled-forward contracts are allowed because they touch only `docs/delivery/spikes/` and `contracts/`, never product code.

An epic may not overlap another on product files.
If a fourth overlap appears, the later epic's spec states why in its section 5, and the captain sees it in the one review.

### Critical path

`#1, #2, #4, #3, #12, #11, #6, #10, #8, #24, smoke, #18, #16`.
The two largest risks are decided earliest: host fit (S2, E2) and the Temporal pattern (S3, spike block).
If S2 fails, the next epic is not opened, and the epics that need no cluster (E3, E4) can continue while the host decision is made.
