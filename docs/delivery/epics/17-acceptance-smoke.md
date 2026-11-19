# Epic #17 pass 1: Smoke gate - breakdown

Status: draft, awaiting spec review
Epic: https://github.com/AndoNorth/wasm-dag-forge/issues/17
Architecture note: none new; the path is `docs/architecture/01-system-context.drawio`, unchanged
Depends on (closed epics): #1, #2, #4, #5, #3, #12, #11, #6, #9, #10, #7, #8, #24

Citations were checked on `main` at `2d1ed1a` on 2026-11-19, and all paths are relative to the repository root.
Tiers as in the sibling specs: T0 host Python with no runtime, T2 the cluster from `just up` plus `scripts/tilt/run.sh ci` on preset `ci`.
Every command takes `nix develop -c`, and every T2 command runs under `/tmp/wdf-cluster.lock`.
Figures marked "estimate" are proposals replaced by the first measured run.

## 0. Decisions

### 0.1 Settled decisions

| # | Decision | Source |
| --- | --- | --- |
| 1 | Smoke is a gate, not a behaviour test, uses a level 0 workload, budget under 5 min, enforced by a timer in the recipe | epic #17 ladder row 3 and key decisions |
| 2 | The gate builds on `wdf_clusterkit` and adds no parallel helper set | captain, via the brief; https://github.com/AndoNorth/wasm-dag-forge/pull/524 (merged) states the purpose "so the smoke gate can build on one kit instead of writing a fourth copy" |
| 3 | `scripts/smoke.sh` and `just smoke` are this epic's stubs; the stub prints `not implemented: owned by #17` and exits 3 | `scripts/smoke.sh:1-5`, `just/acceptance.just:1-2`, `01-dev-environment.md:68,113` |
| 4 | `just cluster-verify` is #2's infrastructure half of smoke and is meant to be called by it | epic #17 ladder row 3, `scripts/cluster-verify.sh:2-3` |
| 5 | The dispatch API is the public surface; the dispatch smoke driver and the admission drive are prior art to consolidate | captain, via the brief |
| 6 | The gate is `just smoke`, one level 0 job through #24, #8, #6, #9, #10, #12 to a stored result, ten consecutive passes before the next epic opens | `method.md:187` |
| 7 | The single-owner cluster rule: take `flock /tmp/wdf-cluster.lock`, refuse a cluster you did not start | `method.md:93`, `02-cluster.md:146-147` |
| 8 | Broker: the smoke publishes through #24, consumes through #8 only, and shows `dlq list` empty | `10-broker.md:260,370` |
| 9 | Workloads: #17 uses the level 0 vectors and `wdf_kernel.check` | `07-workload-toolchain.md:250`, `contracts/workload/README.md:142` |
| 10 | Q1: the gate makes the deployed workflow worker build current after a fresh deploy, through one shared helper that replaces the two copies of `_first_current` (`packages/workflow/src/wdf_workflow/harness/cluster.py:116-134`, `packages/dispatchapi/src/wdf_dispatchapi/smoke/__init__.py:595-613`) and that the harness, the dispatch smoke driver and the gate all call. Why: the gate still passes after a fresh deploy without a change to #6's deploy, and the move deletes a copy instead of adding a third | captain, 2026-11-19 (option b) |
| 11 | Q2: no telemetry step in pass 1; the observability epic #13 adds its own row to `just smoke` as part of its exit. Why: observability is not deployed (`deploy/dummy/obs/module.yaml:1-18`), and scraping raw metrics endpoints is not the epic's assertion | captain, 2026-11-19 (option a) |
| 12 | Q3: "ten consecutive passes" (`method.md:187`) means ten `just smoke` runs back to back on one deploy, plus two fresh `just up`, deploy and `just smoke` cycles. Why: back-to-back runs alone never see a cold first compile, the budget's swing factor, and ten fresh cycles cost over an hour for little more signal | captain, 2026-11-19 (option c) |
| 13 | Q4: CI does not run the gate in pass 1; the exit test records one measured local run and pass 2 decides on a CI job. Why: no runner has ever deployed the services, and `ci` was sized for a runner on paper only (`deploy/presets/ci.yaml:1`) | captain, 2026-11-19 (option a) |
| 14 | Q5: the gate's code lives in a new package `packages/acceptance` (`wdf_acceptance`) on `wdf_clusterkit`, holding small clients (API, Temporal, store, broker) and the gate as an ordered driver `python -m wdf_acceptance.smoke`; pass 2's e2e is pytest over the same clients. Why: the gate is ordered steps where a later step needs an earlier one, so a driver with `ok`/`FAIL` lines matches `cluster-verify` and the existing drivers, and the clients are written once for both passes | captain, 2026-11-19 (option a) |

### 0.2 Questions for the captain

None open: Q1 to Q5 are settled as decisions 10 to 14.

### 0.3 Defaults the lanes build on

| # | Default | Why |
| --- | --- | --- |
| D1 | Preset `ci` only. `dev` and `full` deploy no pool workers (`deploy/workers/compile/module.yaml:25`, `11-workers.md:77`), so `just smoke` on another preset exits 2 with that reason | a gate that cannot pass on a preset should say so, not fail |
| D2 | The job is #24's runnable template: compile `build` of the workload library bundle (entry `wdf_workloads.main`), simulate `sim` with `echo_params()`, aggregate `agg` with `digest_list` (`packages/dispatchapi/src/wdf_dispatchapi/smoke/__init__.py:155-182`). The contract vector `contracts/job/vectors/valid/level0-echo.json` is not runnable (placeholder source, entry `echo`, non-golden args) | the aggregate pool is a layer the gate should see; the template is already measured at 66 s |
| D3 | Assertions read public surfaces: the control API status (it carries `results[]` with `uri` and `sha256`, `packages/dispatchapi/src/wdf_dispatchapi/reads.py:41,69,98`) and the result object fetched with the read-only `export` store identity (`contracts/store/roles.yaml:31-32`). Boundary probes are the only non-API reads, all read-only, and the one write outside the API is the shared helper of decision 10 | epic key decision "public surfaces only, any direct read explicit" |
| D4 | The boundary rows always all run; the job rows run only when every boundary row is ok | the failure points at a layer, and a dead layer does not burn the job timeout |
| D5 | The aggregate's `summary.json` is asserted present with a matching `sha256`, not by value; value checks of the aggregate stay with e2e step 6 | lowest level wins; the gate needs a known answer, which the simulate result gives |

## 1. Goal and non-goals

Goal: after a deploy, one command says whether the platform is healthy, in under 5 minutes, by checking that every layer answers and that one level 0 echo job goes through intake, admission, placement, the compile, simulate and aggregate pools and storage and produces the known answer.
A pass is the precondition for e2e, load and characterization.

Non-goals, deferred:

- The e2e acceptance suite, the negative and control checks, the acceptance fixtures: pass 2 of #17.
- The flake loop recipe and its reporting: pass 2 of #17 (the ten-pass exit of this gate is a command row, decision 12, not a recipe).
- Telemetry rows (epic smoke step 5): #13 (decision 11).
- A CI job that runs the gate: pass 2 of #17 (decision 13).
- The ladder document: lane 0 of this pass writes the smoke row of it only if the captain wants it now; otherwise pass 2, since `method.md:229` claimed it for #1 and it does not exist.
- Any cluster start, deploy or teardown: the gate attaches to a running cluster and never starts one.

## 2. Interfaces

### Consumes

| From | What | Pinned at | Evidence |
| --- | --- | --- | --- |
| #2 | `just cluster-verify`: `ok`/`FAIL` lines, count line, exit 0/1/2/4, honours `WDF_CLUSTER_LOCK_HELD` | `scripts/cluster-verify.sh` on `main` | `:11`, `:26-33`, `:218-228` |
| #24 | `POST /v1/tenants/{t}/batches` (NDJSON), `PUT .../objects/source/{sha256}`, `GET .../jobs/{id}` on control, `/readyz` on both listeners | `contracts/api/routes.yaml` | `:6-19`, `:46-51`, `:69-81` |
| #24 | Host ports `127.0.0.1:18081` dispatch, `127.0.0.1:18080` control | `deploy/host/ports.yaml` | `:4-10` |
| #11 | Level 0 `echo` vector, `wdf_kernel.check`, the workload library bundle | `contracts/workload/vectors/level0/echo/`, `packages/kernel/src/wdf_kernel/check.py:1-8,34-60`, `packages/workloads/src/wdf_workloads/bundle.py` | |
| #12, #4 | Result layout `wdf-results/<tenant>/<batch>/<job>/<seed>/<task>/<attempt>/results.xml`, four buckets | `contracts/artifact/layout.yaml:4-8,15-18` | |
| #7 | `python -m wdf_broker.dlq list`, exit 0/1/2/3 | `packages/broker/src/wdf_broker/dlq.py:1-17` | |
| #6 | Namespace `wdf`, pool queues, `versioning.current`, `versioning.set_current` | `contracts/temporal/naming.yaml:3-19`, `packages/workflow/src/wdf_workflow/versioning.py:99,115` | |
| `wdf_clusterkit` | `Forward`, `kubectl`, `reachable`, `ready`, `until`, `state_dir`, `appdb_password`, `store_credentials`, `Refused`, `INFRA`/`CONTROL`/`WORKERS`/`UP_S` | `packages/clusterkit/src/wdf_clusterkit/__init__.py` on `main` | `:22-171` |

### Produces

| To | What | Where | Shape |
| --- | --- | --- | --- |
| #13, #14, #16, #18, pass 2 | `just smoke` | `just/acceptance.just`, `scripts/smoke.sh` | exit codes and summary below; preset from `WDF_PRESET` (default `ci`) |
| pass 2 | small clients over the API, store, Temporal and broker | `packages/acceptance/src/wdf_acceptance/` (decision 14) | Python module API, documented in the package README |
| the harness, the dispatch smoke driver, the gate | the shared helper that makes the deployed build current when none is (decision 10) | in `wdf_workflow`, the module pinned by R.1 | returns the build made current, or none when one already was; refuses when the deployed builds are not exactly one |

Recipe contract (replaces the stub's exit 3):

| Exit | Meaning |
| --- | --- |
| 0 | every row ok |
| 1 | a row failed, or the 300 s budget ran out (the timeout prints `FAIL budget`) |
| 2 | usage: unsupported preset (D1), or a host tool missing |
| 4 | refused: no cluster `wdf`, or one started for another preset (passed through from `cluster-verify`), or the lock could not be taken within its wait |

Summary, one line per row, in `cluster-verify`'s format so both read alike:

```
ok cluster-verify ci: 10 ok, 0 failed
ok api dispatch and control /readyz ready
...
FAIL job-result echo blocks differ: <first differing key>
smoke ci: 13 ok, 1 failed, 94 s
```

## 3. Design

### 3.1 What exists and what the gate does with it

| Prior art | What it is | Gate takes | Gate leaves |
| --- | --- | --- | --- |
| `just cluster-verify` (`scripts/cluster-verify.sh`) | read-only infra checks, ten rows, own lock unless `WDF_CLUSTER_LOCK_HELD` | called first, as row 1, with `WDF_CLUSTER_LOCK_HELD=1`; its exit 4 becomes the gate's exit 4 | unchanged |
| Dispatch smoke driver, `run` scenario (`smoke/__init__.py:155-189,234-283,392-399`) | uploads the bundle, submits the echo template via NDJSON, polls status every 0.5 s up to `JOB_S = 1800` | the job shape (`template`, `batch`) and the upload and poll calls, moved into `wdf_acceptance`'s API client; the driver then imports them back so there is one copy | its other scenarios (#24's exit rows 10-15) stay in `wdf_dispatchapi.smoke`; closed epic text is not rewritten |
| Harness `matches_echo`, `echo_params`, `ECHO` (`harness/__init__.py:86-88,244,261-271`) | host-side echo check | the gate calls `wdf_kernel.check` on the fetched `results.xml` (the epic's named check); `matches_echo` stays the harness's | unchanged |
| `_first_current` (two copies, `harness/cluster.py:116-134`, `smoke/__init__.py:552-557,595-613`) | makes the deployed build current | the shared helper of decision 10, which R.1 moves both copies into | both drivers call the shared helper |
| Admission drive (`wdf_admission.drive`) | admission journeys on host fake pools | nothing: it fakes the dispatch accept and mutates the admission Deployment env (`drive/__init__.py:4-8,108-125`), which a gate must not do | stays #8's tool |
| `wdf_clusterkit` | forwards, kubectl, readiness, polling, credentials | every cluster call of the gate goes through it | no lock or summary added to it: the shell wrapper owns the lock as `up.sh`/`down.sh` do, and the summary format is the gate's own |
| CI `kind-smoke` (`.github/workflows/kind-smoke.yml`) | `just up`, `just down` on every non-docs PR, 30 min timeout | nothing in pass 1 (decision 13) | unchanged |

### 3.2 Boundary rows, one per layer

All probes are read-only and go through `wdf_clusterkit.Forward` on 127.0.0.1 unless the port is published.
Row 5 is the one exception: on the first run after a fresh deploy it makes the deployed build current (decision 10).

| Row | Layer | Probe | Pass line |
| --- | --- | --- | --- |
| 1 | Cluster and modules | `scripts/cluster-verify.sh` under the held lock | exit 0; its count line copied |
| 2 | Dispatch API | `GET /readyz` on `127.0.0.1:18081` and `:18080` (readiness covers the logins, a store head and Temporal, `deploy/dispatchapi/README.md:88,94-95`) | both `200 {"status":"ready"}` |
| 3 | Intake broker | `python -m wdf_broker.dlq list` as `operator` over a forward of `svc/rabbitmq:5672`; RabbitMQ has no management API (`packages/broker/src/wdf_broker/topology.py:173`) | exit 0 and zero dead letters; the count is recorded before the job and must not rise after it |
| 4 | Admission | Deployment `admission` in `wdf-control` `Available` via `kubectl`; it has no HTTP health route, readiness is the file `/tmp/wdf-ready` (`deploy/admission/README.md:70-71`) | Available, 1/1 ready |
| 5 | Temporal | client connect over a forward of `svc/temporal-frontend:7233`, describe namespace `wdf`, then the shared helper of decision 10 | namespace answers and a build is current; the row names the build, and says when it made it current |
| 6 | Placement | `GET /pools` over a forward of `svc/placement:8080` (`packages/placement/src/wdf_placement/service.py:198-213`) | 200 and the three pools listed |
| 7 | Compile pool | Deployment `compile-worker` in `wdf-workers` `Available` (`deploy/workers/pool.star:48`) | 1/1 ready |
| 8 | Simulate pool | Deployment `simulate-worker` `Available` | 1/1 ready |
| 9 | Aggregate pool | Deployment `aggregate-worker` `Available` | 1/1 ready |
| 10 | Postgres | covered by row 2's `/readyz` (logins connect) and by job row 13 (status is read from the row); no direct SQL | no extra probe (D3) |
| 11 | Object store | `GET /healthz` on a forward of `svc/objectstore:8333` (`deploy/objectstore/README.md:3,14`), plus the four buckets via the objectstore bootstrap check (`deploy/objectstore/README.md:56`, to be confirmed in G.1 that it is read-only) | 200, and `wdf-sources`, `wdf-components`, `wdf-results`, `wdf-checkpoints` present |
| - | Observability | none in pass 1 (decision 11) | |

Rows 4, 7, 8 and 9 partly repeat `cluster-verify`'s `ready` check (`scripts/cluster-verify.sh:154-167`); if G.1 finds that check already covers each module's Deployment, those rows collapse into row 1 and the summary names them there.

### 3.3 The level 0 job rows

Workload: `echo` (`packages/workloads/src/wdf_workloads/level0.py`), the fixture both existing drivers already use.
Known output: one `echo` block `args` with one row per argument key of `{"flag":true,"n":3,"name":"forge","tags":["a","b"]}`, seed 42 (`contracts/workload/vectors/level0/echo/expected.json`).
The literal seed 42 stays in the template because only placeholders bind to the batch entry's seed (`packages/plan/src/wdf_plan/binding.py:126-138`).

| Row | Step | Assertion |
| --- | --- | --- |
| 12 | Upload and submit | build the bundle on the host (`python -m wdf_workloads.bundle`), `PUT` it (201 or 200), submit one job under batch key `smoke-<12 hex>` for tenant `acme` (first `ci` tenant, `contracts/tenancy/tenancy.yaml:5-18`) | answer `accepted` with a job id |
| 13 | It runs and ends | poll control status every 0.5 s through `wdf_clusterkit.until`, bounded by the remaining budget | status passes `running` and ends `succeeded`; `detail.tasks` shows `build` ran or hit the cache |
| 14 | Result | fetch the `sim` result reference from `results[]` with the `export` identity, check its `sha256`, run `wdf_kernel.check results` against the echo vector | sha matches, check exits 0 |
| 15 | Aggregate | the `agg` `summary` reference exists and its object's `sha256` matches (D5) | present and matching |
| 16 | No dead letters | row 3 again | count unchanged |

Unique batch key per run means repeated gates on one cluster never dedup against each other, and the compile may hit the cache on runs after the first, which the epic allows ("a compile ran or hit the cache").

### 3.4 Entrypoint, lock, budget

`scripts/smoke.sh`:

1. Usage: `WDF_PRESET` must be `ci` (D1), else exit 2.
2. Take `flock -w 900 /tmp/wdf-cluster.lock` unless `WDF_CLUSTER_LOCK_HELD` is set, exactly as `scripts/up.sh:19-21` and `scripts/cluster-verify.sh:26-29`; export `WDF_CLUSTER_LOCK_HELD=1` for children.
3. Under `timeout 300` (the budget excludes the lock wait): run `cluster-verify`, then `python -m wdf_acceptance.smoke`.
4. Print the count line with the elapsed seconds, and exit as in section 2.

Attaching to a running cluster is `cluster-verify`'s ownership check (no cluster `wdf`, or `cluster.owner` not equal to the preset, gives exit 4); the gate never starts, deploys or tears down.
The gate relates to a deploy as "`just up`, `scripts/tilt/run.sh ci`, then `just smoke`"; `run.sh` takes no lock today (`scripts/tilt/run.sh:1-13`), which is #2's gap, not this gate's, and is listed under risks.

Budget, estimate: `cluster-verify` 20 to 40 s (never measured), boundary rows about 10 s including forwards, bundle build a few seconds, the job 66 s measured once (`13-dispatch-api.md:806`), so about 2 min with headroom under the 5 min timer.

## 4. Lanes and issues

One worker is enough; the issues are ordered.

| Lane | Owns | Issues | Needs |
| --- | --- | --- | --- |
| 0 | `packages/acceptance/**`, `scripts/smoke.sh`, `just/acceptance.just` (smoke recipe only), root `pyproject.toml` dev group line, `uv.lock` (rebase rule) | 0.1 | none |
| R | the shared helper in `wdf_workflow`, `packages/workflow/src/wdf_workflow/harness/cluster.py` and `packages/dispatchapi/src/wdf_dispatchapi/smoke/__init__.py` (the `_first_current` copies only) | R.1 | none |
| G | `packages/acceptance/src/wdf_acceptance/smoke/**`, `packages/dispatchapi/src/wdf_dispatchapi/smoke/__init__.py` (import-back only) | G.1, G.2 | 0.1, R.1 |

Shared-file rule: lanes R and G both touch `packages/dispatchapi/src/wdf_dispatchapi/smoke/__init__.py`; R.1 merges first and G.2 rebases on it.

- 0.1 Package skeleton and recipe: `wdf_acceptance` on `wdf_clusterkit` with a wheel test, `scripts/smoke.sh` with lock, preset check, timer, exit codes and the count line, the driver printing `ok`/`FAIL` rows; T0 tests of the summary and exit mapping.
- R.1 One shared helper that makes the deployed build current when none is (decision 10): move the two `_first_current` copies into it, the harness and the dispatch smoke driver call it, their existing tests still pass; a T0 test of the helper's three outcomes (already current, made current, not exactly one deployed build) against a fake client and a fake `kubectl`.
- G.1 Boundary rows 1 to 11, each with a T0 test against a fake answer, row 5 calling R.1's helper; T2 evidence: one run on a healthy cluster, and one run with the placement Deployment scaled to 0 showing `FAIL placement` and exit 1 (a single manual check, not a negative suite).
- G.2 Job rows 12 to 16, the API client moved from the dispatch smoke driver with the driver importing it back and its existing tests still passing; T2 evidence: three consecutive passes.

## 5. Order, stubs and spikes

0.1 and R.1 first, in either order, then G.1 and G.2 (one worker, in order).
No stubs: every dependency is merged.
No spikes: the job path is measured (row 12 of #24).
No stub contract test exists for `scripts/smoke.sh` today (grep finds none), so lane 0 adds the T0 test of the exit mapping instead.

## 6. Test delta

- T0: summary format, exit mapping, each probe's verdict against a canned answer, the shared helper's outcomes, the moved API client's existing tests.
- T2: `just smoke` itself; no pytest at T2 in this pass (decision 14).
- Untested: telemetry (decision 11), cold start on a CI runner (decision 13).

## 7. Exit test

Run by a worker who did not write the lanes, on a clean export of `main`, under the lock.

| Row | Command | Expected |
| --- | --- | --- |
| 1 | `just ci` | green |
| 2 | `just up`, `scripts/tilt/run.sh ci` | cluster up, Tilt `SUCCESS` |
| 3 | `just smoke` | exit 0, every row `ok`, row 5 names the build it made current, elapsed under 300 s and recorded as the measured local run of decision 13 |
| 4 | `just smoke` ten times in a row, plus two fresh cycles of rows 2 and 3 (decision 12) | ten consecutive exit 0, and both fresh cycles exit 0; elapsed of each recorded |
| 5 | `WDF_PRESET=dev just smoke` | exit 2 with the preset reason |
| 6 | `just down`, then `just smoke` | exit 4, `refused: no kind cluster named wdf` |

## 8. Risks

- `cluster-verify` has never been timed; if it is slow it eats the budget. Mitigation: measure in G.1; the budget can exclude it if the captain prefers.
- A cold first compile is the budget's swing factor (epic risks); 66 s is one measurement.
- The gate is not purely read-only: on the first run after a fresh deploy, row 5 makes the deployed build current (decision 10). It changes Temporal only when no build is current, and refuses when the deployed builds are not exactly one, so a rollout in progress fails the row instead of being overridden.
- Open intake issues may make the job late, not wrong: https://github.com/AndoNorth/wasm-dag-forge/issues/520 (reconciler left rows unpublished for about 195 s) would push row 13 past the budget if the single job's publish takes the reconciler path.
- `scripts/tilt/run.sh` takes no lock, so a deploy can overlap a gate run; the gate cannot fix that, #2 can.
- No runner has deployed the services, so the gate's fit on a CI runner is unknown until pass 2 (decision 13).
- The bucket check via the objectstore bootstrap must be confirmed read-only (G.1); if not, the row uses `HEAD` per bucket with the `workflow` identity for three buckets and leaves `wdf-checkpoints` to `cluster-verify`'s readiness of the bootstrap module.

## 9. Left to the acceptance pass (pass 2)

- The e2e suite in pytest over `wdf_acceptance`'s clients, with Postgres, Temporal history, CloudEvent and trace assertions.
- Two-chain acceptance job, aggregate value check, cache-hit check, all negative and control checks.
- Telemetry assertions (once #13 lands; #13 adds the smoke row).
- Whether CI runs the gate, decided from the measured local run of exit row 3 (decision 13).
- The flake loop recipe and the ladder document if not written in lane 0.
- The epic's open question on direct Postgres reads (this pass reads none).
