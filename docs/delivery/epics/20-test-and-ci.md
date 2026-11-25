---
status: draft
date: 2026-11-24
updated: 2026-11-24
issues: []
scope: [test-tooling, ci, test-ladder]
---

# Test tooling and CI: one way to run each tier, every test runs somewhere, efficacy measured - breakdown (draft)

Status: draft, captain's five decisions recorded (section 0), awaiting the spec review
Epic: not filed, the URL is filled in when the epic is filed
Architecture note: none new. `docs/acceptance-ladder.md` stays the home of the levels, and DOC-1 refines it. No component, data path or contract is added, so `docs/architecture/` and `docs/contracts.md` do not change.
Depends on (closed epics): #17 pass 1 (the smoke gate), #1 (the dev environment, which owns the justfile and CI jobs)
Beside: the platform seams epic (`docs/delivery/epics/19-platform-seams.md`, pull request https://github.com/AndoNorth/disyard/pull/681). Section 7 lists every lane of that epic this one touches, and section 3.4 lists what this one does not repeat.

Citations were checked on `main` at `dc51212` on 2026-11-24, and every path is relative to the repository root.
Counts and times marked "measured" come from the runs in section 3, and times marked "estimate" are replaced by the first measured run of the issue that owns them.
The draft was written read-only: no code was written, no cluster was used, and the integration tier was not run.
The spec review agents of `docs/delivery/method.md` were not run, because the captain runs them on the landed spec.
Section numbers differ from the epic template: template section 0 (decisions) is section 0, section 1 is sections 1 and 2, section 2 (interfaces) is section 4.5, section 3 (lanes) is section 5, section 4 (issues) is section 6, section 5 (order) is section 7, section 6 (test delta) and section 7 (exit test) are section 9, section 8 (parallelism) is section 11 and section 9 (closure) is section 12.
The `spec-template` skeleton maps as: Problem is section 1, Goals and Non-Goals are section 2, Architecture / Design is section 4, Validation is section 9, References is the last section, Open Questions is section 0 and Out of Scope is section 10.

## Summary

The captain asked to consolidate the test tooling and the CI the way the platform seams epic consolidates the service plumbing, and to find where tests can be simpler and prove more.
The answer, in seven lines:

1. The suite is 4,597 tests: 3,766 unit, 788 integration, and 43 that no recipe, workflow or runbook selects, so they never run (section 3.1).
2. The cause is structural: five markers, three selection mechanisms and the same expression typed in `scripts/test.sh`, `scripts/pre-pr.sh` and the docs.
   One tier table and one rule for markers that must be asked for fix the cause, and a self-test keeps it fixed (TIER lane).
3. The speed problem is one avoidable file in the unit tier and one structural fact in the integration tier.
   `scripts/tilt/test_tiltfile.py` is the tail of every unit run, and the integration tier is 78% of the CI test time and runs serially.
   The first is an XS issue, and the second is a spike, because the T1 lock was built for a shared host (UNIT and CI lanes).
4. CI has no required check, no scheduled run, no JUnit data, no per-test timeout, and builds a six-node cluster that never runs a job (section 3.3).
   The lanes add data first (CI-1), then a timeout, balanced shards, and a weekly run of everything that never ran, which is deferred.
5. Efficacy: the suite is better than feared on mocks (3 tests monkeypatch three or more times) and on named behaviour (80 of 82 codes), and weaker at the edges: the real Kubernetes API, the acceptance clients, outages at T1, and any measure of whether the tests catch changes (EFF lane).
6. Eight options are rejected with reasons (section 4.7): a flake quarantine, a coverage gate, CI test selection, a lint merge, porting the shell self-tests, a behaviour-name check, and two more.
7. Delivery: 5 lanes, 17 issues (3 XS, 8 S, 6 M), at most three workers at once, five decisions recorded (section 0).
   Four issues start now, the weekly lanes are deferred, and the rest waits for the seams gate (section 7).
   Nothing here changes a contract or a service.

## 0. Decisions for the captain

The captain answered the five questions of the draft.
Each answer is recorded here as resolved, and the lanes below are written to match.

### 0.0 Resolved calls

**Q1. Make `check` a required status check on `main`?** Resolved: require `check` only.
Evidence: `gh api repos/AndoNorth/disyard/branches/main/protection` answers 404 and so does `rulesets`, so a red or running `check` does not stop a merge today.
`check` is the job that sums the others (`.github/workflows/ci.yml:94-107`), so shard renames (CI-3) never change the required name.
This is a repository setting the captain makes, not a file in a pull request, and no issue of this epic carries it (B9).

**Q2. Should `just smoke` run on a hosted runner?** Resolved: no.
No cluster job ever gates a pull request, and smoke stays a command run by hand.
The spike of smoke on a hosted runner is dropped.
Evidence for the risk, kept for the record: `state-modules` timed out once in its last 12 pushes (30 minutes, run 45460852877), and `ci` needs 12,032 MiB of a runner's about 15,983 MiB.

**Q3. Is a scheduled workflow acceptable?** Resolved: yes, weekly, report-only, and not worked on now.
The captain wants it to run weekly and does not want to work on it now, so the weekly workflow lanes (CI-4 and the mutation pilot EFF-4) are deferred: written and sized here, not started.
When started, the workflow would run the 43 tests that run nowhere, the integration tier three times per shard to measure flakes, and the mutation pilot, and it costs about 1 to 2 runner hours a week (estimate: three repeats of the 2,612 s of integration time).

**Q4. May the lanes that only measure start before the e2e of issue #682 lands?** Resolved: start the measuring lanes now.
The start-now set is UNIT-1, CI-1, CI-2 and the spike CI-S1 (the integration tier under xdist).
The rest waits for the seams gate: the seams spec holds every lane except its library lane until `just e2e` runs a real suite (its decision Q5), because its refactor needs the e2e as a regression net.
This epic changes no service code, and the four start-now issues only add data, speed or guards.
They touch `scripts/test.sh`, `pyproject.toml` and `.github/workflows/` only in lines that pull request https://github.com/AndoNorth/disyard/pull/689 also touches, so whoever lands second rebases (section 5).
UNIT-2, UNIT-3, TIER, CI-3, EFF and DOC wait as section 7 says, and CI-4 and EFF-4 are deferred by Q3.

**Q5. Run a mutation pilot, and add a development dependency for the timeout?** Resolved: both.
`pytest-timeout` becomes a dev dependency (CI-2).
`mutmut` runs only in the weekly workflow through `uvx` and never enters `uv.lock` (EFF-4), so it adds no runtime or CI-gate dependency.
The mutation result is report-only with no gate, and the pilot runs in the weekly workflow, so it is deferred with Q3.

### 0.1 Defaults the lanes build on

| # | Default | Why |
| --- | --- | --- |
| D1 | The `just` recipe names, the `scripts/*.sh` names, the exit codes (0, 1, 2, 3 stub, 4 refused, 5 nothing collected is success) and the CI job names `changes`, `checks`, `test (<shard>)`, `check` do not change | the docs, the hooks and the required check name them |
| D2 | The ladder's levels are the vocabulary, and T0, T1, T2 are the lane issue template's tiers, as the seams spec D4 maps them | the ladder is the approved document |
| D3 | The tests that run today keep running in the same tier, and a count before and after is pasted in every issue that moves tests | the seams spec relies on the same regression net |
| D4 | No `contracts/` change, no service code change | this epic is about the tools |
| D5 | The only new third-party dependencies are `pytest-timeout` (Q5) and the weekly-only `mutmut` | the rest is the standard library, `uv`, pytest and xdist as already pinned |
| D6 | New CI work reports first and gates after: weekly jobs are report-only, and no job joins the `check` sum without three green runs | the evidence of section 3.3 is one failure in 12 for the one cluster job that exists |

### 0.2 Cleanup calls this epic resolves, and the ones it leaves

The seams spec restates the twelve cleanup calls of the cleanup scout report in its section 0.2.
This epic resolves or uses four of them and leaves the rest.

| # | Call (short) | Here |
| --- | --- | --- |
| 4 | Gate for `slow`, `isolated`, `cluster` tests | Resolved as TIER-2: markers that must be asked for are deselected by one rule, and no marker is left without a tier |
| 5 | Test infrastructure in service images | Resolved as DOC-2, after the seams service lanes |
| 6 | CI shape and shard count | Resolved in part: CI-3 derives the shard list from measured durations, and the count stays six until CI-S1 reports |
| 10 | T0/T1/T2 versus ladder levels | Assumed: the ladder is canonical (D2), as in the seams spec |
| 1, 2, 3, 7, 8, 9, 11, 12 | | not touched, see the seams spec |

## 1. Problem

A test that exists but is run by no recipe proves nothing, and that is the state of 43 tests today.
The tools that run the tests grew one epic at a time, so each tier is selected by a string typed where it was needed, and a marker added later (`isolated`, `cluster`, soon `e2e`) is handled by its own mechanism or not at all.
The CI around them does what the first epics needed: it runs two tiers and some checks, on six shards, with no data about which test is slow or which one fails twice, no per-test timeout, no required check, no scheduled run, and a six-node cluster that is brought up and torn down and never given a job.
The result is a suite that is large (4,597 tests), mostly fast, and slow in a few known places: the unit tier is held up by one file, and the integration tier, 17% of the tests, is 78% of the test time and runs serially because its lock was built to protect a shared development host.
What the suite cannot yet say is how well it would catch a change, because nothing measures that, and what it never runs against the real thing: the Kubernetes API, the clients that smoke and e2e stand on, and outages caused on purpose.

## 2. Goals and non-goals

Goals, each testable:

1. Every test is selected by a named tier, and a marker that no tier selects fails a self-test: after TIER-2, `scripts/itest.sh --tier isolated --collect-only` lists the 37 isolated tests, `scripts/test.sh --collect-only -q | tail -1` and the tier table agree, and the self-test fails when a marker is added to `pyproject.toml` without a tier row.
2. The pytest selection expression of each tier is written once (`scripts/lib/tiers.sh`): `grep -rn 'not integration' scripts` prints only that file.
3. The unit tier's wall time is no longer set by one file: after UNIT-1 the `rest` shard's unit step in CI is below the 389 s measured, to a target set from the CI-1 per-test data, and three consecutive CI runs of `main` show the same pass counts.
4. Every shard publishes a JUnit file and its 30 slowest tests, and a hung test fails in at most 5 minutes (CI-1, CI-2).
5. Deferred (Q3): a weekly run executes the isolated tier, the slow tier and three repeats of the integration tier per shard, and its first run produces a per-test failure table (CI-4).
6. The spike has a written result with a binary pass line (CI-S1), so the captain decides on numbers.
7. The three fakes without a conformance test get one that runs against the real thing (EFF-1, EFF-2), and the T1 helper can freeze or stop a container without a hand-written `docker` call (EFF-3).
8. Deferred (Q3, Q5): the mutation score of four decision modules is known (EFF-4).
9. The ladder, the validation matrix and the dev setup page agree on commands, counts and times, and each fact is written in one of them (DOC-1).

Non-goals, each with where it goes:

- Service plumbing, test fixtures that move into `dsy_*` testing modules, the `just seam` level and the marker registration block: the platform seams epic (section 3.4 lists each lane).
- The e2e suite, its scenarios and its wrapper: #17 pass 2 (issue #682).
- Failure scenarios and recovery drills beyond the two T1 twins of EFF-3: epic #15.
- Load, performance and soak: epics #14 and #16.
- A flake quarantine, a coverage gate, CI test selection, a lint merge, porting the shell self-tests and a behaviour-name check: rejected, section 4.7.
- Any change to what a test asserts, except the vector tests of UNIT-2, which keep their assertions and change how the loader is called.
- Runner hosting, a self-hosted runner, or a CI vendor change.

## 3. Current state

The evidence, measured on `main` at `dc51212` and on the last 100 CI runs.
The scout report that holds the commands and their output is not in the repository, so each number is restated with its source.

### 3.1 The tiers

| Expression | Collected |
| --- | --- |
| `not integration and not slow` (unit, `scripts/test.sh:14`) | 3,766, of which 59 skip |
| `integration` (`scripts/itest.sh:19`) | 788 |
| `isolated` | 37 |
| `slow and not integration` | 6 |

The 43 that run nowhere: 9 artifact client fault scenarios against a real store, 6 orphan sweep tests, 2 broker restart tests, 20 tests of the T1 helper itself (`dsy_itest`), and 6 large-input tests.
Six `cluster` tests in `packages/obs` skip twice (`packages/obs/tests/conftest.py:14-20`, then on a missing environment variable that no recipe sets).

Markers and who selects them: `integration` and `slow` by the expression strings of three scripts; `pair` and `isolated` by the `dsy_itest` plugin (`packages/itest/src/dsy_itest/fixtures.py:48-77`); `cluster` by a conftest in `packages/obs`; `e2e` (pull request https://github.com/AndoNorth/disyard/pull/689) by an edit of `scripts/test.sh` only, so `scripts/pre-pr.sh:31` will differ.

### 3.2 Time

CI, run 45460852877 on `main` (clean 4-core runners):

| Shard | Unit s | Integration s | Job s |
| --- | --- | --- | --- |
| workflow | 52 | 526 | 633 |
| admission | 25 | 554 | 633 |
| dispatchapi | 69 | 405 | 525 |
| worker | 107 | 313 | 482 |
| artifacts | 90 | 568 | 714 |
| rest | 389 | 247 | 691 |
| sum | 732 | 2,612 | 3,678 |

Integration is 17% of the tests and 78% of the test time.
Wall time of `ci` is the slowest shard, 11 to 13 minutes.
The `rest` unit step is 389 s because `scripts/tilt/test_tiltfile.py` (23 tests, each a `tilt` call) is one `--dist loadfile` work unit.
Local, unit tier, one clean run each on an 8-core host with load average 10 to 13: 196 s with `loadfile` and 176 s with `worksteal`, 3,707 passed and 59 skipped in both.
The slowest tests are wheel builds (22 files, `uv build` of a closure each), per-vector `check.sh` processes (`packages/events`, `packages/jobdoc`, `packages/stages`), and toolchain tests (`packages/runtime`, `packages/compile`).

### 3.3 CI

| Workflow | Triggers | Time | Blocks merge |
| --- | --- | --- | --- |
| `ci` | push to `main`, every pull request | 11 to 13 minutes | no |
| `kind-smoke` | same | 2 minutes (`just up`, `just down` only) | no |
| `state-modules` | `main`, storage paths | 7 to 8 minutes, one 30 minute timeout in 12 pushes | no |
| `scan` | `main`, every pull request, weekly | 4.5 to 6 minutes | no |

`main` has no branch protection and no ruleset.
The only schedule is the weekly `scan`.
No step passes `--junitxml`, `--durations` or a timeout, and `uv.lock` has no `pytest-timeout`, coverage tool or mutation tool.
In the last 100 `ci` runs: 95 success, 3 failure (all real defects), 1 cancelled.

### 3.4 What this epic does not repeat from the seams epic

| Seams lane | What it already does | Here |
| --- | --- | --- |
| L0-3 | `just seam`, level 3a, the `seam` marker, `scripts/seam.sh`, the ladder row 3a | the tier table has a `seam` row, nothing more |
| RIG-R-3 | one marker registration block and `--strict-markers` | TIER-2 builds on it and owns only the selection rule |
| TS-1, TS-2, TS-3 | `db`, `login`, `shelf`, OTLP receiver, broker fixture, `free_port`, the dev server factory, `Rig` | EFF-2 consumes them |
| RIG-A, RIG-D, RIG-W, RIG-R | migrate the consumers' conftests onto the shared fixtures | not touched. The `test_wheel.py` files are carved out of them (section 5) |
| TOOL-1, TOOL-4 | one cluster lock, exit 4 everywhere | CI-S1 runs on a hosted runner and takes no local lock |
| TOOL-2 | the doctor self-test line in `scripts/ci.sh` | TIER-1 edits `scripts/ci.sh` around it |
| ADM, DIS, WFL, WRK, PLC, OBS | adopt `dsy_servicekit`, including the boot tests that replace hand-patched `main()` tests | DOC-2 waits for them because it edits the same `pyproject.toml` files |

### 3.5 Efficacy

Property tests exist in 13 files across 9 of 34 packages.
Fakes with a conformance test that also runs against the real thing: the broker, the dispatch gateway, the workflow control and placer fakes, and the observability fake (whose real half never runs, 3.1).
Fakes without one: the placement Kubernetes API fake (`packages/placement/tests/kube/conftest.py:34`), and the acceptance clients' fakes (`packages/acceptance/tests/clients/test_temporal.py:28,40`, `test_postgres.py:10`).
`packages/acceptance` and `packages/placement` have no integration tests.
Three tests monkeypatch three or more attributes in the whole suite.
80 of 82 codes of four behaviour taxonomies are named by a test, and the other two are table-driven.
No outage helper exists in `dsy_itest`, and nothing measures the tests' ability to catch a change.

## 4. Design

### 4.1 One tier table

`scripts/lib/tiers.sh` defines `tier_args <tier>`, which prints the pytest arguments of a tier:

| Tier | Arguments | Entry |
| --- | --- | --- |
| `unit` | `-n auto --dist worksteal -m "not integration"` | `scripts/test.sh`, `just test` |
| `integration` | `-m integration` | `scripts/itest.sh`, `just itest [domain]` |
| `isolated` | `-m isolated` | `scripts/itest.sh --tier isolated` |
| `slow` | `-m slow` | `scripts/test.sh --tier slow` |
| `cluster` | `-m cluster` | the `kind-smoke` job (EFF-1) |
| `e2e` | `-m e2e` | `scripts/e2e.sh` (#17 pass 2) |
| `seam` | `-m seam` | `scripts/seam.sh` (seams L0-3), row added when it merges |

`scripts/test.sh`, `scripts/itest.sh`, `scripts/pre-pr.sh` and `scripts/ci.sh --shard` source it.
A tier name stays a flag of the existing script and no recipe is added: `just itest` already forwards its arguments.
`scripts/tiers.test.sh` is a self-test, run in `ci.sh --checks`, that reads the markers of `pyproject.toml` and fails when a marker has no tier row or a tier row names an unregistered marker.

### 4.2 One rule for markers that must be asked for

The plugin that already deselects `isolated` (`packages/itest/src/dsy_itest/fixtures.py:68-77`) takes a set `GATED = {isolated, slow, cluster, e2e}` and deselects (not skips) every test that carries one unless `-m` names that marker, with a parsed expression in place of the token scan of `names_isolated`.
The `obs` conftest skip hook is deleted.
The effect: the unit expression reduces to `not integration`, a new gated marker is one word in one set, and a deselected test is counted in the pytest summary line instead of hiding in a skip count.
A `cluster` test then runs only when selected, and still skips when its environment variable is unset, which is correct and visible.

### 4.3 Unit-tier cost

- UNIT-1: `--dist worksteal`.
  The risk is a module-scoped fixture running twice, which costs CPU and is measured, and a test that relied on file-level order, which three green CI runs and one local run with `-p no:randomly` (there is none) cannot prove, so the criterion is three green CI runs of `main` plus the local count.
- UNIT-2: the vector tests of `packages/events`, `packages/jobdoc` and `packages/stages` call the loader function in-process, and each contract keeps one subprocess smoke of its `check.sh` that asserts the exit code and the `reason:` line.
  Behaviour kept: every vector is still checked with the same expected reason.
- UNIT-3: one fixture builds every workspace wheel once per pytest invocation into a directory shared by the xdist workers (a file lock beside `tmp_path_factory.getbasetemp().parent`), and the 22 wheel tests install from it into their own clean venv.
  Behaviour kept: each test still installs into a clean venv and imports away from the repository.
  The home is the existing `dsy_itest` plugin, unless the issue shows a root `conftest.py` is simpler.

### 4.4 CI shape

- CI-1: `scripts/ci.sh --shard` passes `--junitxml=<dir>/<shard>.xml --durations=30`, and the `test` job uploads the XML as an artifact.
- CI-2: `timeout = 300` in `[tool.pytest.ini_options]`, `pytest-timeout` in the dev group, and a `@pytest.mark.timeout(n)` on each test the first JUnit data shows above 120 s.
- CI-3: `scripts/ci-shards.sh --suggest <junit dir>` prints a balanced layout by greedy packing of package durations, and the committed list in the script stays hand-reviewed.
  Shard names that exist today are kept where possible, because `test (<shard>)` job names appear in the history, and the required check is the summing `check` job.
- CI-4 (deferred, Q3): `.github/workflows/weekly.yml`, `schedule` weekly at 03:17 UTC on Mondays and `workflow_dispatch`, with jobs `isolated`, `slow` and `flake-sample` (a matrix of the six shards, each running the integration tier three times, JUnit uploaded), a final step that prints a per-test failure table to the job summary, and no effect on `check`.
- CI-S1 (spike): the integration tier of the `artifacts` shard under `-n 2` on a hosted runner.
  What changes is how `dsy_itest` hands out its two lock files to xdist workers (`session.py:32`, `fixtures.py:80-96`): one session per worker holding one file, and `pair` files holding both, which serialises them.
  Pass line: the same 172 integration tests pass in at least 30% less test time over 5 runs with no new failure.

### 4.5 Interfaces

Consumes:

| From | What | Pinned at | Used until it exists |
| --- | --- | --- | --- |
| #17 pass 2 (issue #682, pull request 689) | the `e2e` marker and `scripts/e2e.sh` | merged `main` | TIER-1 leaves the `e2e` row out |
| Seams L0-3 | the `seam` marker, `scripts/seam.sh` | merged `main` | the `seam` row is added by whoever lands second |
| Seams RIG-R-3 | the single marker block and `--strict-markers` | merged `main` | TIER-2 registers nothing and reads the block |
| Seams TS-1, TS-3 | `db`, `login`, the dev server factory | merged `main` | EFF-2 waits |
| `dsy_itest` | `Session`, `container`, the plugin | `packages/itest` on `main` | none |

Produces:

| To | What | Where | Shape |
| --- | --- | --- | --- |
| every test script | the tier table | `scripts/lib/tiers.sh` | `tier_args <tier>` prints arguments, exit 2 on an unknown tier |
| `just itest`, the weekly workflow | `--tier <name>` | `scripts/itest.sh`, `scripts/test.sh` | the first argument, default `integration` and `unit` |
| the plugin | `GATED` | `packages/itest/src/dsy_itest/fixtures.py` | a frozenset of marker names |
| CI | JUnit artifacts | `.github/workflows/ci.yml`, `weekly.yml` | `junit-<shard>.xml`, 30 day retention |
| the captain | spike results | `docs/delivery/spikes/S<n>/` | pass or fail, numbers, decision |

### 4.6 Efficacy lanes

- EFF-1: the placement reader's assertions are written once as a body that runs against `FakeApi` and, marked `cluster`, against the real API server of the kind cluster through `kubectl proxy`, in a new step of `kind-smoke.yml` between `just up` and `just down`.
- EFF-2: T1 tests for the acceptance clients, in `packages/acceptance/tests/clients/`, against the dev Temporal server (TS-3) and a Postgres container (TS-1), asserting what `FakeClient`, `FakeHandle` and `FakeConn` assume.
- EFF-3: `Session.container()` handles gain `stop()`, `pause()` and `resume()`, and two of the runbook drills get a T1 twin that asserts the observable recovery: broker down (`docs/runbooks/drills/broker-down.md`) and object store down (`object-store-down.md`), because both dependencies are T1 containers.
  The scenario catalogue stays with epic #15.
- EFF-4 (deferred, Q3, Q5): `mutmut` through `uvx` in the weekly workflow on four modules, report-only: `packages/workflow/src/dsy_workflow/failures.py`, `budgets.py`, `packages/dispatchapi/src/dsy_dispatchapi/control_table.py` and `packages/admission/src/dsy_admission/policy.py`, plus one `pytest --cov` line coverage report of the same packages in the job summary.
  Chosen because each is pure decision logic that a wrong change can break silently, and the first three have property or table tests, which makes the score informative.

### 4.7 Options considered and not taken

| Option | Why not |
| --- | --- |
| A flake quarantine (a `flaky` marker, a non-blocking job, auto-issues) | 0 of the 3 failures in 100 runs were flakes. CI-4 measures the rate once started, and the item is revisited when the weekly run shows more than 2% |
| A line coverage gate | low signal for this suite, adds minutes. One report inside EFF-4 |
| Select tests by changed package in CI | 27% of commits are eligible, wall time does not drop, and contract and deploy changes (52%) need the full run anyway |
| Merge `lint.sh` and the flake check | each catches things the other does not, and the duplicate costs about 20 s |
| Port the five `*.test.sh` to pytest | 360 lines, stable, a fine home already |
| A "code is named by a test" check for the taxonomies | 80 of 82 named, and the other two are table-driven, so it would be a false positive |
| Rebalance shards before UNIT-1 | at most 2.5 minutes, and the `rest` tail would still set the wall time |
| Raise the T1 lock count for everybody | the lock protects a shared host. CI-S1 changes how a runner uses it, not the host rule |

## 5. Lanes

One worker per lane.
Paths are exclusive, and a path two lanes need is a shared file with a rule below the table.

| Lane | Owns (paths, exclusive) | Issues | Needs | Workers |
| --- | --- | --- | --- | --- |
| TIER | `scripts/lib/tiers.sh`, `scripts/tiers.test.sh`, the pytest lines of `scripts/{test,itest,pre-pr}.sh` and the `--shard` branch of `scripts/ci.sh`, `packages/itest/src/dsy_itest/fixtures.py`, `packages/itest/tests/`, `packages/obs/tests/conftest.py` | TIER-1, TIER-2 | pull request 689 merged, seams RIG-R-3 merged | 1 |
| UNIT | `packages/events/tests/`, `packages/jobdoc/tests/`, `packages/stages/tests/` (vector files only), every `packages/*/tests/test_wheel.py`, the wheel fixture (`packages/itest/src/dsy_itest/wheels.py`, new), the `-n auto --dist` token of `scripts/test.sh` and `scripts/pre-pr.sh` (UNIT-1 only) | UNIT-1, UNIT-2, UNIT-3 | UNIT-1 none, UNIT-2 none, UNIT-3 none | 1 |
| CI | `.github/workflows/ci.yml`, `.github/workflows/weekly.yml` (new, deferred), `scripts/ci-shards.sh`, `scripts/ci-shards.test.sh`, the `timeout` line of `pyproject.toml`, `docs/delivery/spikes/S8/**` (spike results) | CI-1, CI-2, CI-3, CI-4 (deferred), CI-S1 | CI-1, CI-2, CI-S1 none; CI-3 needs CI-1, UNIT-1; CI-4 needs TIER-2, CI-1 | 1 |
| EFF | `packages/placement/tests/kube/`, `packages/acceptance/tests/clients/`, `packages/itest/src/dsy_itest/session.py` (the handle methods only), `packages/itest/tests/test_faults.py` (new), `packages/broker/tests/suite/`, `packages/artifactclient/tests/` (twin of the object store drill only), `.github/workflows/kind-smoke.yml` | EFF-1, EFF-2, EFF-3, EFF-4 (deferred) | EFF-1 none; EFF-2 needs seams TS-1, TS-3; EFF-3 needs TIER-2; EFF-4 needs CI-4 | 1 |
| DOC | `docs/acceptance-ladder.md` (a section "Tiers"), `docs/runbooks/validation-matrix.md`, `docs/dev-setup.md` (the test and CI sections), `docs/delivery/method.md` (the verifier convention paragraph only), the dependency lines of `packages/{migrate,dispatchapi,broker,workflow,temporal}/pyproject.toml` | DOC-1, DOC-2 | DOC-1 needs TIER, CI-3, seams L0-3 and pull request 689; DOC-2 needs seams ADM, DIS, WFL, WRK merged | 1 |
| EXIT | `docs/delivery/epics/20-test-and-ci.md` Closure | EXIT-1 | all | 1 |

Shared-file rules:

- `scripts/test.sh`: pull request 689 (`e2e` in the expression), UNIT-1 (the distribution token) and TIER-1 (sourcing the table) all edit it.
  Whoever lands second rebases, and TIER-1 deletes the expression so a conflict resolves to "call `tier_args`".
- `scripts/pre-pr.sh`: TIER-1 and UNIT-1 edit its pytest lines, and nothing else owns it.
- `pyproject.toml`: seams RIG-R-3 owns the marker block, CI-2 adds one `timeout` line and the `pytest-timeout` dev entry, UNIT-3 and the seams lanes add workspace lines.
  `uv.lock` conflicts are never resolved by hand, and `uv lock` is rerun after rebase.
- `packages/*/tests/test_wheel.py`: UNIT-3 owns them, and the seams RIG lanes carve them out so the two epics never edit one file.
- `scripts/ci.sh`: seams TOOL-2 adds one line (the doctor self-test), TIER-1 edits the shard branch and adds the tier self-test line, CI-1 adds the JUnit arguments.
- `docs/acceptance-ladder.md`: seams L0-3 owns row 3a, #17 pass 2 owns the e2e rows, DOC-1 owns a section "Tiers" and the measured numbers.
- `.github/workflows/ci.yml`: CI-1, CI-2 and CI-3 edit it in disjoint steps, and the seams epic does not touch it.
- An out-of-lane edit is a review rejection (method, lane isolation rule 6).

## 6. Issues

Format as in the seams spec: scope, owned paths, acceptance criteria that name a command and an observable result, a verifier, consumes and produces, needs, size and kind.
Sizes: XS under 30 diff lines, S under 150, M 150 to 600, L above 600.
Kind: mech means the existing tests are the safety net and no judgement is needed, judge means a per-site decision is part of the work, spike means the result is a written decision.
Every issue's "done" is the lane issue template's: `just pre-pr` green, or `just ci` when it asks for the full run, the CI `check` job green, pasted output of the named commands, and nothing outside the owned paths changed.
After approval these blocks are replaced by `#NNN` links.

### Lane TIER: one table, one rule

**TIER-1 The tier table** - S, mech
- Scope: `scripts/lib/tiers.sh` with `tier_args`; `scripts/test.sh`, `scripts/itest.sh` (a first argument `--tier <name>`), `scripts/pre-pr.sh` and `scripts/ci.sh --shard` call it; `scripts/tiers.test.sh` reads the markers of `pyproject.toml` and fails on a marker without a tier row; the self-test runs in `ci.sh --checks`.
- Criteria: `grep -rn 'not integration' scripts` prints only `scripts/lib/tiers.sh`; `nix develop -c scripts/test.sh --collect-only -q | tail -1` and `scripts/itest.sh --collect-only -q | tail -1` print the same counts as before (3,766 and 788 on `dc51212`, adjusted by the tests the merged pull request 689 adds, counts pasted before and after); `scripts/tiers.test.sh` exits 1 after a throwaway marker is added to `pyproject.toml` and 0 after it is removed (both outputs pasted); `scripts/itest.sh --tier nonsense` exits 2.
- Verifier: `scripts/tiers.test.sh`, the two collect counts.
- Consumes / produces: consumes the marker block; produces `scripts/lib/tiers.sh`.
- Needs: pull request https://github.com/AndoNorth/disyard/pull/689 merged (it edits `scripts/test.sh`), seams RIG-R-3.

**TIER-2 One rule for markers that must be asked for** - S, judge
- Scope: `GATED` in `dsy_itest.fixtures`, deselect not skip, with the marker expression parsed (an AST over the `-m` text, or pytest's own `Expression` matcher) in place of the token scan; the `obs` conftest skip hook deleted; the `isolated`, `slow`, `cluster` and `e2e` rows of the table (TIER-1) selected by name; `scripts/itest.sh --tier isolated` and `scripts/test.sh --tier slow` work.
- Criteria: `scripts/itest.sh --tier isolated --collect-only -q | tail -1` prints `37 tests collected` and `scripts/test.sh --tier slow --collect-only -q | tail -1` prints `6 tests collected`; `scripts/test.sh -q | tail -1` shows the six `cluster` tests as deselected and the skip count drops by 6 (counts pasted, 59 before); `pytest -m "not (isolated or integration)"` and `-m "not isolated"` select as the token scan did (a table test of 8 expressions); `packages/itest/tests/` holds the new unit tests.
- Verifier: `scripts/test.sh packages/itest`, the two collect counts.
- Consumes / produces: consumes TIER-1 and the marker block; produces `GATED`.
- Needs: TIER-1.

### Lane UNIT: the unit tier's cost

**UNIT-1 `--dist worksteal` for the unit tier** - XS, mech
- Scope: the distribution token in `scripts/test.sh` and `scripts/pre-pr.sh` (in `tier_args` once TIER-1 has landed), nothing else.
- Criteria: `time nix develop -c scripts/test.sh` before and after on the same host, both pasted with `uptime`, with the pass count unchanged; on `main` after merge, three consecutive `ci` runs show the `rest` shard unit pytest summary below the 389 s measured, with the target set from the CI-1 per-test data (the 200 s figure is an estimate), and with the same pass counts as the table of section 3.2; the user time of both runs is pasted, so the CPU cost of repeated module fixtures is on record.
- Verifier: the two timings, the three CI run urls.
- Consumes / produces: consumes xdist 3.8.0 as pinned; produces nothing.
- Needs: none (it may start before the seams gate, Q4).

**UNIT-2 Vector tests call the loader in-process** - M, judge
- Scope: the loops over vectors in `packages/events/tests/test_instrumentation.py`, `test_contract_vectors.py`, `packages/jobdoc/tests/test_check_stub.py` and `packages/stages/tests/test_stages_contract.py` call the loader function the `check.sh` script calls, and each contract keeps one subprocess test of its `check.sh` that asserts one valid file exits 0 and one invalid file exits 1 with the `reason:` line.
- Criteria: the five files' test counts are unchanged (pasted) and every vector still asserts the same reason code; `scripts/test.sh packages/events packages/jobdoc packages/stages --durations=10` shows the five slowest of those packages together under 20 s (estimate, from about 50 s in a clean local run and 145 s in a contended one); `grep -rn 'check.sh' packages/events/tests packages/jobdoc/tests packages/stages/tests` lists one subprocess call per contract.
- Verifier: that command, before and after.
- Consumes / produces: consumes the loader entry points named in `contracts/*/check.sh`; produces nothing.
- Needs: none.

**UNIT-3 One wheel build per pytest run** - M, judge
- Scope: one session fixture builds the workspace wheels once (`uv build --wheel --all-packages` or a per-package loop, whichever the prototype shows faster) into a directory shared by the xdist workers under a file lock; the 22 `test_wheel.py` files install from it into their own clean venv; the closure helper `workspace_closure` stays one function.
- Criteria: `scripts/test.sh --durations=30` shows the wheel tests' total time (setup plus call, summed from the listing) under half of today's (119 s in the top 60 of a clean local run), both pasted; each wheel test still imports away from the repository (the `-I` probes are untouched); `grep -rn 'uv", "build"' packages` lists one file.
- Verifier: that command, before and after, and `scripts/test.sh packages/dispatchapi packages/workflow`.
- Consumes / produces: consumes `uv build`; produces the fixture.
- Needs: none. It owns the `test_wheel.py` files and the seams RIG lanes carve them out.

### Lane CI: data, guards, shape

**CI-1 JUnit and durations from every shard** - XS, mech
- Scope: `scripts/ci.sh --shard` adds `--junitxml` and `--durations=30` to both pytest calls; the `test` job uploads `junit-<shard>.xml` with `actions/upload-artifact`, 30 days.
- Criteria: a pull request run shows 6 artifacts and the 30 slowest tests in each shard log (urls pasted); `just ci --shard worker` locally writes the XML.
- Verifier: the run url.
- Consumes / produces: produces the JUnit artifacts.
- Needs: none (may start before the seams gate, Q4).

**CI-2 A per-test timeout** - XS, mech
- Scope: `pytest-timeout` in the dev group, `timeout = 300` and `timeout_method = "thread"` in `[tool.pytest.ini_options]`, `@pytest.mark.timeout(n)` on the tests CI-1 or the run of section 3 shows above 120 s.
- Criteria: a throwaway test that sleeps 400 s fails in at most 310 s with a stack dump, both under `-n auto` (output pasted, test not committed); `scripts/test.sh` and `scripts/itest.sh` pass with the same counts; `uv lock --check` clean.
- Verifier: the two commands.
- Consumes / produces: consumes Q5; produces one dev dependency.
- Needs: none (may start before the seams gate, Q4).

**CI-3 Shards from measured durations** - S, mech
- Scope: `scripts/ci-shards.sh --suggest <dir>` reads JUnit files and prints a balanced layout; the committed `shards` list is updated from it and reviewed; `scripts/ci-shards.test.sh` covers `--suggest` on a fixture.
- Criteria: over 3 consecutive `main` runs after merge the slowest shard is at most 1.15x the mean (today 714 s against 613 s, 1.16x); the `check` job is unchanged and no shard name is dropped without a note in the pull request.
- Verifier: the three run urls, `scripts/ci-shards.test.sh`.
- Consumes / produces: consumes JUnit from CI-1; produces the layout.
- Needs: CI-1, UNIT-1.

**CI-4 Weekly workflow (deferred, Q3)** - S, judge
- Scope: `.github/workflows/weekly.yml`, weekly cadence on Mondays: `isolated` (the isolated tier), `slow`, `flake-sample` (a matrix of the six shards, three repeats of the integration tier each, `--junitxml` per repeat), and a final job that prints per-test failure counts to the job summary; no dependency of `check` on it.
- Criteria: a `workflow_dispatch` run completes and its summary names every test that failed in any repeat; each red `isolated` or `slow` test has an issue linked in the pull request (the first run is expected to be red, D6); the workflow takes no cluster lock and starts no cluster.
- Verifier: the run url.
- Consumes / produces: consumes the `--tier` flag of TIER-2 and the JUnit arguments of CI-1; produces the weekly run.
- Needs: TIER-2, CI-1.

**CI-S1 Spike: the integration tier under xdist on a runner** - M, spike
- Question: do two xdist workers, each holding one of the two T1 lock files, cut the integration time of a shard by at least 30% without new failures?
- Pass line: `artifacts` shard, 172 tests, 5 runs, same pass set, time at least 30% lower, no failure not seen at `-n0`.
- Result file: `docs/delivery/spikes/S8/xdist-integration.md` with the numbers and the decision, and the throwaway code under `docs/delivery/spikes/S8/code/`.
- Blocks: a follow-up issue that changes `dsy_itest` lock handling, filed only on a pass.
- Needs: none. It runs on hosted runners through `workflow_dispatch`, so the local T1 lock is not taken (M1 of the scout list).

### Lane EFF: efficacy

**EFF-1 The placement reader against the real Kubernetes API** - S, mech
- Scope: the assertions of `packages/placement/tests/kube/test_reader.py` and `test_snapshotter.py` become a shared body run against `FakeApi` and, marked `cluster`, against the real API server through `kubectl proxy`; a step in `.github/workflows/kind-smoke.yml` runs `scripts/test.sh --tier cluster packages/placement` between `just up` and `just down`.
- Criteria: the unit tier count is unchanged and the new cluster test is deselected there (counts pasted); a `kind-smoke` run is green and its log shows the cluster test passed (url); adding 20 s or less to the job (estimate); the body fails on a deliberately wrong field name (output pasted).
- Verifier: the run url.
- Consumes / produces: consumes TIER-2's `cluster` rule once merged (until then it runs with `-m cluster`); produces the conformance body.
- Needs: none for the body, TIER-2 for the `--tier` form.

**EFF-2 The acceptance clients against real backends at T1** - M, mech
- Scope: `packages/acceptance/tests/clients/`, new integration tests for the Postgres client and the Temporal history client against a container and the dev server, each asserting what `FakeConn`, `FakeClient` and `FakeHandle` assume.
- Criteria: `scripts/itest.sh acceptance` runs and passes (output pasted, count 0 before); a deliberately wrong assumption in a fake (for example a different row order in `FakeConn`) makes the paired real test fail (output pasted); the unit tests of the package are unchanged.
- Verifier: `scripts/itest.sh acceptance`, `scripts/test.sh packages/acceptance`.
- Consumes / produces: consumes seams TS-1 (`db`, `login`) and TS-3 (dev server factory); produces integration tests.
- Needs: seams TS-1, TS-3.

**EFF-3 T1 fault helpers and the twins of two drills** - M, judge
- Scope: `stop()`, `pause()` and `resume()` on the container handle of `dsy_itest` (the `docker` call lives in one place, `packages/itest/src/dsy_itest/session.py`); the hand-written `docker stop` in `packages/dispatchapi/tests/control/test_retrier.py:50` calls the helper; two scenarios marked `integration` and `isolated`: broker down and back (`docs/runbooks/drills/broker-down.md`) and object store down and back (`object-store-down.md`), each asserting the observable recovery the drill lists.
- Criteria: `scripts/itest.sh --tier isolated packages/itest` passes the helper tests, including that a paused container is resumed when the test body fails (output pasted); the two scenarios pass at T1 and fail when the dependency stays down (a negative run pasted); `grep -rn '"docker", "stop"' packages` prints only the helper.
- Verifier: the commands above.
- Consumes / produces: consumes `Session` and the drill pages; produces the helper methods and two scenarios. The scenario catalogue stays with epic #15, which consumes the helper.
- Needs: TIER-2.

**EFF-4 Mutation pilot and one coverage report (deferred, Q3, Q5)** - M, judge
- Scope: a weekly job (in CI-4's workflow, a separate job) runs `uvx mutmut` on `dsy_workflow/failures.py`, `budgets.py`, `dsy_dispatchapi/control_table.py` and `dsy_admission/policy.py` against their package tests, and a `pytest --cov` run of the same packages, both to the job summary; no dependency of `check`, no entry in `uv.lock` for `mutmut`; `docs/delivery/spikes/S8/mutation-pilot.md` records the scores and the surviving mutants worth a test.
- Criteria: one weekly run produces the scores and the coverage table (url); the result file lists, per module, the score and at most five surviving mutants with the test each one would need; the job finishes in under 60 minutes (estimate) or the pilot narrows to two modules.
- Verifier: the run url and the result file.
- Consumes / produces: consumes CI-4's workflow; produces the pilot result.
- Needs: CI-4. Decisions Q3 and Q5.

### Lane DOC: one home per fact

**DOC-1 One home per fact in the ladder docs** - S, mech
- Scope: `docs/acceptance-ladder.md` gains a section "Tiers" that holds the tier table of 4.1 and the measured counts and times; `docs/runbooks/validation-matrix.md` drops its time claims that the ladder now holds ("tens of minutes", "about 10 minutes") and keeps "which check for which change"; `docs/dev-setup.md:294` is corrected to the exit 4 text of `:96`; the verifier convention paragraph of `docs/delivery/method.md` names `scripts/lib/tiers.sh`; the `just` names stay.
- Criteria: `scripts/check-links.sh` passes; `grep -rn 'not integration and not slow' docs` prints nothing; every command that appears in the three pages exists as a recipe (a one-line check pasted, `just --summary` against the names in the pages); the ladder states the measured unit and integration times with the run url.
- Verifier: the two greps and `scripts/check-links.sh`.
- Consumes / produces: consumes TIER, CI-3, seams L0-3 and pull request 689 edits of the same pages; produces the section.
- Needs: TIER-2, CI-3, seams L0-3, pull request 689.

**DOC-2 Test infrastructure out of the runtime dependencies** - S, judge
- Scope: `dsy-itest` and `pytest` move from `[project] dependencies` to an optional `test` extra or the dev group in `packages/{migrate,dispatchapi,broker,workflow,temporal}/pyproject.toml`; modules under `src/` that import `dsy_itest` (`packages/broker/src/dsy_broker/testing/container.py`, `packages/temporal/src/dsy_temporal/corpus.py`, the two bench modules) import it lazily or move to the test extra.
- Criteria: `uv tree --package dsy-workflow` and the four others no longer list `pytest` or `dsy-itest` (outputs pasted); each service image still builds (`just ci --checks` and the image builds of `just scan images` run; output pasted); `scripts/itest.sh broker` and `scripts/itest.sh workflow` still pass.
- Verifier: the `uv tree` outputs and the two itest runs.
- Consumes / produces: consumes the seams adoption lanes' `pyproject.toml` edits; produces smaller runtime dependency sets.
- Needs: seams ADM, DIS, WFL, WRK merged.

### Lane EXIT

**EXIT-1 The exit test and Closure** - S, mech
- Scope: section 9.2, run by a worker who did not write the lanes; the Closure of this file filled; the architecture note not touched.
- Criteria: every row of 9.2 has its evidence file, and Closure names the commit, the CI run and the numbers against section 3.
- Verifier: the evidence files pasted in the pull request.
- Needs: every issue above.

## 7. Order, dependencies and spikes

### 7.1 Dependencies on the platform seams epic

| This epic | Waits for | Why |
| --- | --- | --- |
| TIER-1 | pull request 689 merged, seams RIG-R-3 | both edit `scripts/test.sh` and the marker block |
| TIER-2 | TIER-1 | the table names the tiers |
| EFF-2 | seams TS-1, TS-3 | the `db`, `login` and dev server factories |
| DOC-2 | seams ADM, DIS, WFL, WRK | the same `pyproject.toml` files |
| DOC-1 | seams L0-3, pull request 689, TIER-2, CI-3 | the same documents |
| UNIT-3 | none, and the seams RIG lanes carve out `test_wheel.py` | disjoint files by rule |
| every issue not listed | nothing | disjoint files |

This epic adds no wait to the seams epic.
The one thing the seams lanes get from this epic is faster and observable test runs, so the regression net of the refactor improves while it is being used.

### 7.2 Order

```
Phase 0, start now (Q4)       UNIT-1   CI-1   CI-2   CI-S1
Held to the seams gate:
Phase 1, after 689 + RIG-R-3  TIER-1 -> TIER-2       UNIT-2   UNIT-3
Phase 2                       CI-3 (needs CI-1, UNIT-1)    EFF-1   EFF-3 (needs TIER-2)
Phase 3, after seams TS       EFF-2 (needs TS-1, TS-3)
Phase 4, after seams services DOC-2 (needs ADM, DIS, WFL, WRK)   DOC-1 (needs TIER-2, CI-3, L0-3, 689)
Deferred (Q3, Q5)             CI-4 (needs TIER-2, CI-1) -> EFF-4 (needs CI-4)
Last                          EXIT-1
```

Phase 0 is the four issues of Q4.
Everything after it waits for the seams gate, and CI-4 and EFF-4 stay deferred until the captain starts them.

### 7.3 Spikes

| Id | Question | Pass line | Blocks |
| --- | --- | --- | --- |
| CI-S1 | integration tier under xdist on a runner | at least 30% faster, same pass set, 5 runs | a follow-up issue on `dsy_itest` lock handling |

It writes only under `docs/delivery/spikes/S8/` and creates a workflow file that runs by `workflow_dispatch`, which `docs/delivery/method.md` allows alongside the active epic.

### 7.4 Stubs

None. Every issue changes a real script, test or workflow.

## 8. Behaviour changes, listed separately

Each is a change a user or a pipeline can see.
None changes a contract, an exit code or a recipe name.

| Id | Change | Issue | What to check |
| --- | --- | --- | --- |
| B1 | Tests marked `isolated`, `slow`, `cluster` or `e2e` are deselected, and counted as deselected, in every tier that does not name them. The `cluster` tests of `packages/obs` stop appearing as skips, so the skip count drops by 6 | TIER-2 | a dashboard or script that reads the skip count |
| B2 | The unit tier distributes by test, not by file, so a module-scoped fixture may run on two workers, and test order inside a file is not kept | UNIT-1 | CPU time, and a test that assumed file-level order |
| B3 | A test that runs longer than 300 s fails with a stack dump instead of hanging to the job limit. One new dev dependency, `pytest-timeout` | CI-2 | a test that legitimately runs longer gets `@pytest.mark.timeout` |
| B4 | `ci-shards.sh` layout changes, so which tests share a runner changes. Job names `test (<shard>)` change only where a shard is added or dropped, and the required check is `check` | CI-3 | any doc or script that names a shard |
| B5 | A weekly workflow appears (deferred, Q3) and its first run is expected to be red, because 43 tests have not run for weeks | CI-4 | a red test becomes an issue, not a revert |
| B6 | The vector tests of three packages call the loader in-process, so the shell wrapper `check.sh` is exercised once per contract, not once per vector | UNIT-2 | a change that breaks only the wrapper's per-vector argument handling would be caught by the smoke, not each vector |
| B7 | `kind-smoke` gains a step that reads the real Kubernetes API, 20 s or less, and fails if the reader's assumptions are wrong | EFF-1 | a failing step names the field |
| B8 | `dsy-itest` and `pytest` leave the runtime dependency closure of five packages | DOC-2 | a script outside `tests/` that imported `dsy_itest` |
| B9 | `main` requires `check` (Q1) | none, a repository setting the captain makes | a merge with a red `check` becomes impossible |

## 9. Validation

### 9.1 Test delta

Added: `scripts/tiers.test.sh` (a marker without a tier row fails), the `GATED` table tests (8 expressions), the in-process vector tests and one wrapper smoke per contract, the wheel fixture's own test, the helper tests of EFF-3, the placement real-API body, the acceptance T1 tests, the `--suggest` fixture test of CI-3.
Moved: the 43 unrun tests join a tier (`isolated` and `slow`), and 6 `cluster` tests change from skip to deselect.
Removed: nothing, except the per-vector subprocess calls of UNIT-2 and the repeated wheel builds of UNIT-3.
Not tested here, and why: the spike (a result, not code), the weekly workflow's scheduling (the `workflow_dispatch` run is the evidence), the mutation score (a report), both deferred by Q3.

### 9.2 Exit test

Run by a worker who did not write the lanes, on a clean export of `main` after all issues merge, and the output of each step is saved as `exit-test/<n>.txt`.

| # | Command or observation | Expected |
| --- | --- | --- |
| 1 | `nix develop -c scripts/test.sh --collect-only -q`, `scripts/itest.sh --collect-only -q`, `scripts/itest.sh --tier isolated --collect-only -q`, `scripts/test.sh --tier slow --collect-only -q` | the four counts sum to the full `pytest --collect-only` total, which section 3.1 gives as 4,597 plus the tests the seams and #17 pass 2 epics added |
| 2 | `scripts/tiers.test.sh` after adding a throwaway marker, then after removing it | exit 1, then exit 0 |
| 3 | `grep -rn 'not integration' scripts docs` | only `scripts/lib/tiers.sh` |
| 4 | the last three `ci` runs on `main`: per-shard unit and integration times from the logs | the slowest shard at most 1.15x the mean, the `rest` unit step below 389 s and the target set by UNIT-1 |
| 5 | the weekly run of the previous week, only if CI-4 was started (Q3) | completes; its summary names every failed test; each red test has an issue |
| 6 | a throwaway 400 s sleeping test under `-n auto` | fails in at most 310 s with a stack dump |
| 7 | `docs/delivery/spikes/S8/` | the CI-S1 result merged, stating pass or fail with numbers |
| 8 | `uv tree --package dsy-workflow` and the four others | no `pytest`, no `dsy-itest` |
| 9 | the `kind-smoke` run on `main` | its log shows the placement real-API test passed |
| 10 | `docs/acceptance-ladder.md`, `validation-matrix.md`, `dev-setup.md` | every command exists, and the measured times match step 4 within 20% |

## 10. Out of scope

What comes next, not what never happens:

- The follow-up issue that changes `dsy_itest` lock handling, filed only if CI-S1 passes.
- Running smoke in a scheduled workflow: smoke stays by hand and never gates a pull request (Q2).
- Promoting the isolated tier to a pull request shard, decided after four weekly runs, once CI-4 is started (Q3).
- Tests this epic's pilots find missing, filed as issues against the package that owns the code.
- The failure scenario catalogue of epic #15, which uses the helper of EFF-3.
- A flake quarantine, revisited when the weekly run shows a rate above 2% (section 4.7).

## 11. Parallelism and cost

Five lanes, one worker each, at most three at once: Phase 0 has UNIT-1 and the CI lane's CI-1, CI-2 and CI-S1, Phase 1 has TIER and the rest of UNIT, Phase 2 adds EFF-1, EFF-3 and CI-3, and the DOC lane runs last. CI-4 and EFF-4 wait for the captain.
The cluster is not used by any worker.
The spike runs on hosted runners: the cost is runner minutes, an estimated 5 runs of one shard for CI-S1 (about 1 runner hour).
The weekly workflow, when started, costs about 1 to 2 runner hours a week (estimate: three repeats of the 2,612 s of integration time, plus the isolated and slow tiers and the mutation pilot).
Estimates of effort, from the sizes: 3 XS, 8 S and 6 M issues, which is about two weeks for one worker and about one week with three, of which the four start-now issues are a small part, and the seams gate decides when the second half can start.

## 12. Closure (filled at close)

Commit, CI run url, what the numbers of section 3 became, what moved to which epic.

## References

- Platform seams breakdown spec, `docs/delivery/epics/19-platform-seams.md`, pull request https://github.com/AndoNorth/disyard/pull/681.
- Merged CI speed work this builds on: https://github.com/AndoNorth/disyard/pull/684 (six shards, cache key, run cancellation, docs rule), https://github.com/AndoNorth/disyard/pull/685 (exit 4 when the T1 lock wait runs out), https://github.com/AndoNorth/disyard/pull/688 (pytest-xdist for the unit tier).
- In flight: https://github.com/AndoNorth/disyard/pull/689 (issue #682, the `just e2e` wrapper).
- `docs/acceptance-ladder.md`, `docs/runbooks/validation-matrix.md`, `docs/dev-setup.md`, `docs/delivery/method.md`, `docs/delivery/templates/epic-breakdown.md`, `docs/delivery/epics/17-acceptance-smoke.md` (decision 13).
- Code: `scripts/test.sh`, `scripts/itest.sh`, `scripts/pre-pr.sh`, `scripts/ci.sh`, `scripts/ci-shards.sh`, `scripts/ci-paths.sh`, `scripts/affected.py`, `packages/itest/src/dsy_itest/{fixtures,session}.py`, `packages/obs/tests/conftest.py`, `.github/workflows/{ci,kind-smoke,state-modules,scan}.yml`, `flake.nix`, `pyproject.toml`.
- CI runs cited: 45460852882 (per-shard times), 45460852877 (`state-modules` timeout), 45277259323, 45466455400 and 44966660378 (the three failures of the window).
