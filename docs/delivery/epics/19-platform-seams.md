# Platform seams: domains, a shared access library and a seam smoke level - breakdown (draft)

Status: draft, awaiting the captain's spec review
Epic: not filed, the URL is filled in when the epic is filed
Architecture note: `docs/architecture/21-domains-and-access.md` (new, written in lane L0)
Depends on (closed epics): the runtime epics whose code it touches (#3 to #12, #24) and #17 pass 1 (the smoke gate)
Gate for every lane except L0: issue #682 (the `just e2e` wrapper of #17 pass 2, in flight) delivers a passing `just e2e`, decision Q5.
The fixtures of pass 2 merged in pull request https://github.com/AndoNorth/disyard/pull/679 (issue #674), but `scripts/e2e.sh` still exits 3.
Pass 2 edits `packages/acceptance`, `packages/workloads` and `docs/acceptance-ladder.md`, so section 6 gives the shared-file rules.

Citations were checked on `main` at `ee88fc7` on 2026-11-24, and every path is relative to the repository root.
The draft was written read-only: no code was written, no test ran and no cluster was used.
The spec review agents were not run, because the captain runs them on the landed spec.
Section numbers differ from the template: template section 2 (Interfaces) is section 2.4 here, the domain map and the library are sections 2 and 3, and template sections 3 to 9 are sections 6 to 12 here.
The exit test is section 10 here, where `docs/delivery/method.md:112` points at "section 7" of the template.

## Summary

The captain asked for the repo to be cleaned by structure.
The answer, in six lines:

1. Domains: the code splits into 11 domains (section 2.1).
   Every cross-service call goes through one of five dependency kinds: Postgres, Temporal, RabbitMQ, the object store and the contract files.
   Three of those five already have an owning library (`dsy_jobstate`, `dsy_eventstore`, `dsy_artifactindex` for Postgres tables, `dsy_artifactclient` for the store, `dsy_broker` for RabbitMQ).
2. Call patterns: what is not owned by anyone is the plumbing around the calls: how a connection is opened, how a Temporal client is built, how a service reads its environment, signals ready and stops (section 2.2 and 2.3).
   That plumbing exists as 4 to 7 hand-written copies, and 11 of the copies have drifted in ways that change behaviour.
3. Shared library: one new small package, `dsy_servicekit` (config, lifecycle, Postgres connect, a test kit), plus three additions to packages that already own the topic: `dsy_temporal.connect`, `dsy_clusterkit.logins` and the test-support modules of `dsy_workflow`, `dsy_migrate`, `dsy_storecheck` and `dsy_obs` (section 3).
   No contract changes, no change of an existing public name, no change of an environment variable, an exit code or a metric name, except the seven behaviour items H1 to H7 that decision Q4 keeps as separate issues.
4. Tests: each service proves the library contract with a short test over a kit in `dsy_servicekit.testing`, the library has its own unit and integration tests, and a layering test keeps the dependency direction (section 4).
5. Seam smoke: a new ladder level 3a, `just seam`, tier T1 with no cluster, that boots every service and runs a worked example of a new workflow added as a module without a core edit (section 5).
   The existing cluster smoke is unchanged and is run at two checkpoints, together with the e2e of issue #682.
6. Delivery: 17 lanes, 41 issues, at most four workers at once (sections 6 to 12).
   Only the library lane L0 and its own tests may start before the e2e of issue #682 lands, and every other lane waits for it (decision Q5).
   Twenty-six issues are mechanical with no judgement, and eight carry a behaviour change that decision Q4 keeps as separate issues, each starting from a reproduction.

## What I did

- Read the cleanup scout report, appendices A to D in full and the rest by headings, as the brief said.
  That report is a working document of the captain and is not in the repository, so every call, row and finding this spec depends on is restated where it is cited (sections 0.2 and 0.3).
- Read `docs/delivery/method.md`, `docs/delivery/templates/epic-breakdown.md`, the lane issue template, `docs/acceptance-ladder.md`, `docs/contracts.md`, the validation matrix and the approved spec of #17 pass 1 for style.
- Derived the domain map from the code, not from the epic docs: the `[project] dependencies` of all 34 packages (a Python script over `tomllib`), `git grep` of every import of `psycopg`, `temporalio`, `dsy_broker`, `dsy_artifactclient`, `dsy_jobstate`, `dsy_eventstore` and `dsy_artifactindex` in `packages/*/src`, and the call sites named in section 2.1.
- Re-verified the scout's load-bearing claims against `main` (section 13), and found one conflict the scout did not see (decision note C4 in section 13).
- Wrote no code and ran no test, no cluster command, no container and no `just` recipe.
  Every behavioural claim is from reading code, and the two defects the scout marked "suspected" are still suspected here, so their lanes begin with a reproduction.

## 0. Decisions for the captain

Two questions are still open, with a recommendation each.
The five questions of the draft were answered by the captain on 2026-11-24, and their answers are recorded below as resolved.

### Open questions

**O1. Should a different business domain extend this codebase or get a new one?**
Options: extend this codebase through the module seam of section 5 only, or start a separate codebase for it.
Recommendation: extend only through the seam, with no edit of a core package, and decide again after the worked example of MOD-2 shows what a module costs.
Default if no answer: wait for the worked example.

**O2. Is the public surface of `dsy_servicekit` in section 3.2 approved as drafted?**
Options: approve it at this review, or change it before L0-1 starts.
Recommendation: approve it here, so that L0-1 and L0-2 carry no design judgement, and a later signature change is a pull request against the README list.
Default if no answer: L0-1 builds the surface of section 3.2 as written.

### Resolved decisions

#### Q1. What does "a new workflow" mean for the extension proof?

The word covers four different things in this code, and they have four different costs.

| Kind | What is added | Core files edited today | Evidence |
| --- | --- | --- | --- |
| A | A new job template: a job document over the three task types | none, it is data | `contracts/job/registry.json` lists the task types |
| B | A new workload: a guest module that a simulate task runs | none, `dsy_workloads` is a bundle the compile task builds | `packages/workloads/README.md` |
| C | A new task type with its own pool: for example a fourth type beside compile, simulate and aggregate | 17 source files that name the three types as literals, plus `contracts/job/registry.json`, `contracts/temporal/pools.yaml` and a pool Deployment | section 5.1 |
| D | A new Temporal workflow module that runs beside `JobWorkflow` on the same worker host | `dsy_workflow.service.serve` builds exactly three workers by hand and has no extension point (`packages/workflow/src/dsy_workflow/service/__init__.py:219-241`) | section 5.1 |

Options were: prove D and B, prove A, B and D and only measure C, or build C as well.
Decision: prove D and B.
D is the one kind that has no seam today, and the phrase "stays modular for extension" is about a seam.
B and A are shown as the second and third worked examples, which cost no core change.
C is measured with a pinned touch list and left to a later epic, because making the task type set open touches the job contract, the plan models and the placement table, and that is a contract change.
The extend-or-new-codebase question for another business domain stays open as O1.

#### Q2. One new package or none?

Options were (a) one new package `dsy_servicekit` for config, lifecycle, Postgres connect and the test kit, plus `dsy_temporal.connect` and `dsy_clusterkit.logins` in packages that own those topics, or (b) no new package, spreading the pieces over `dsy_obs`, `dsy_temporal` and `dsy_clusterkit`.
Decision: (a), named `dsy_servicekit`.
`dsy_obs` is the telemetry helper and its README says so, and lifecycle and environment reading are not telemetry.
`dsy_clusterkit` is host side and pulls `boto3` through `dsy_storecheck`, which a service image should not need for a ready file.
`dsy_servicekit` depends on `psycopg` only, which every service already has.
The name pairs with `dsy_clusterkit` (the host side of the same plumbing).

#### Q3. Where does the extension proof run?

Options were (a) tier T1 only, with containers and no cluster, as ladder level 3a, or (b) also a cluster row, which needs the example module installed in the workflow image and so an edit to `deploy/workflow/image.sh` and its Tiltfile module.
Decision: (a), the integration tier only, with no cluster row now.
The cluster is single-owner, a module in the image is a deploy change that this epic would have to make to the core, and the cluster half has nothing to prove that level 3a does not, apart from the image build.
The cluster half is listed as a follow-up.

#### Q4. How are behaviour changes handled?

The adoption of the library is mechanical and keeps every observable behaviour.
Seven items change a behaviour while they fix a real defect, and they are kept as separate issues so that the mechanical diff stays reviewable and each one can be reverted alone.

| Item | Change | Why it is not mechanical |
| --- | --- | --- |
| H1 | Postgres connects in admission, the dispatch retrier and reconciler and the reaper get a default `connect_timeout` | a bounded connect fails loops that block today, so each caller's retry path needs a look (scout B5) |
| H2 | The dispatch Temporal client gets the metrics runtime, and admission and dispatch build theirs through `dsy_obs.metrics.runtime` | exported bucket bounds of `dsy_event_bytes` change, which a dashboard may read |
| H3 | The reaper worker gets a `graceful_shutdown_timeout` | a pass in flight at SIGTERM is drained instead of cut |
| H4 | The worker reads `DSY_TEMPORAL_NAMESPACE` instead of `DSY_NAMESPACE` | the pod must roll with the module, code and Starlark change in one commit |
| H5 | Dispatch exports metrics on 9464 like the other five services, not 9090 | a runbook or port-forward example that names 9090 changes |
| H6 | The state-inputs exporter removes its ready file on stop, as the other five services do | a probe that waited on the file now sees it vanish at stop (scout F3) |
| H7 | Every cluster script exits 4 when its lock wait runs out, and `tilt/run.sh` (`ci` and `soak`) and `canary.sh` take the lock | `up.sh`, `down.sh`, `nuke.sh` and `cluster-verify.sh` exit 1 today (`up.sh:27`), so a wrapper that tests for exit 1 changes (cleanup call 2) |

Options were: ship each as its own issue after the mechanical adoption of its service, fold them into the adoption issue, or skip H4 and H5 as cosmetic.
Decision: separate issues, each starting with a reproduction, with H4 and H5 last in their lanes.
H6 is OBS-2 and H7 is TOOL-4.
H5 is dropped if any runbook other than `deploy/dispatchapi/README.md` names 9090 for dispatch (not checked, section 13).
The Prometheus forward `19090:9090` in the runbooks is another service and does not trigger the drop.

#### Q5. Start before the e2e suite exists?

`just e2e` still exits 3 (`scripts/e2e.sh`): the fixtures of #17 pass 2 merged in pull request #679 (issue #674), and the wrapper that makes it run is issue #682, in flight.
A refactor of all six services would otherwise rest on the unit and integration tiers, the seam smoke added here, `just smoke` and the API scenarios of `python -m dsy_dispatchapi.smoke` (`pause-resume`, `cancel-before-start`, `restart`).
Options were: start now with two cluster checkpoints, or hold the service lanes until pass 2 delivers the e2e.
Decision: wait for the e2e of issue #682 before the service lanes.
Only the library lane L0 and its own tests may start earlier, because they touch no service.
Every other lane starts after `just e2e` runs a real suite, so that the e2e is part of the regression net of the refactor and of both checkpoints (section 8.1).

### 0.1 Defaults the lanes build on

| # | Default | Why |
| --- | --- | --- |
| D1 | `contracts/` does not change, and each lane's pull request shows an empty `git diff --stat origin/main -- contracts/` | the captain asked for a library "outside the contracts and interfaces already defined" |
| D2 | Every existing public name stays importable. A renamed exception or helper leaves a one-line alias in its old module | lanes stay disjoint, because no consumer outside the lane needs an edit |
| D3 | Environment variable names, exit codes (2 for configuration, 1 for a refused tenancy profile), ready-file paths, metric names and drain constants do not change, except the seven items H1 to H7 of Q4 | the deploy render tests pin them, for example `deploy/workflow/test_render.py:119` pins `DSY_TEMPORAL_NAMESPACE` |
| D4 | The ladder's levels are the vocabulary (level 1 unit, level 2 integration), and the tier labels T0, T1, T2 of the lane issue template map to them as T0 = level 1, T1 = level 2, T2 = level 3 and up, except that the seam smoke, level 3a, is tier T1 (containers, no cluster) | the ladder is the document the captain approved, and the T0/T1/T2 glossary is open as cleanup call 10 |
| D5 | No new third-party dependency | `dsy_servicekit` needs `psycopg` only, and the test kit uses the standard library |
| D6 | The lock helper is one definition, `cluster_lock <exit_code>`, with no flag: `smoke.sh` passes 4 and the other callers keep their current exit until H7 moves them to 4 | `docs/acceptance-ladder.md:63-64` and the #17 spec decision 18 |

### 0.2 The twelve cleanup calls

The twelve calls of the cleanup scout report (its section 3, which is not in the repository, so each call is restated here) stay the captain's, and are not renumbered.
This spec resolves, assumes, depends on or leaves each one as follows.

| # | Call (short) | Here |
| --- | --- | --- |
| 1 | Delete the lane-stub scaffold | Not resolved, not needed. This epic uses no stubs: the library lands unused and is proven by a real toy service (lane L0), which is the method's rule 3 in a simpler form |
| 2 | Cluster lock policy: lock `tilt/run.sh` and `canary.sh`, exit 4 everywhere | Resolved by the captain: lock both, exit 4 everywhere. Lane TOOL builds the one lock helper with the exit as a parameter (TOOL-1), and TOOL-4 applies the policy, which is a behaviour change (H7) because four scripts exit 1 on a lock timeout today |
| 3 | T1 lock timeout behaviour | Resolved by the captain: a queued T1 run refuses with a distinct exit. It matters here, because `just seam` and the library's integration tests hold the T1 lock, and a queued second run must not read as a test failure. The change itself is in `packages/itest`, which no lane of this epic owns, so it stays a cleanup item and the seam smoke relies on it |
| 4 | Gate for `slow`, `isolated`, `cluster` tests | Not resolved. The seam tests are ordinary `integration` tests, and none uses `isolated` |
| 5 | Test infrastructure in service images | Not resolved, and this design does not add to it: `dsy_servicekit` has no `dsy_itest` dependency. Note that `dsy_temporal`, `dsy_broker`, `dsy_dispatchapi` and `dsy_workflow` carry `dsy-itest` in their runtime dependencies today (`packages/*/pyproject.toml`), so `dsy_temporal.connect` ships beside it |
| 6 | CI shape and shard count | Not resolved. New packages join the `rest` shard by default (`scripts/ci-shards.sh`, the last shard runs every unnamed path) |
| 7 | Architecture notes | Resolved for this area. Lane L0 writes `docs/architecture/21-domains-and-access.md` and its diagram, the first note under the rule of `AGENTS.md`. The other areas stay open |
| 8 | Unlinked diagram pairs | Not touched |
| 9 | Dummy fixtures and retired verifier machinery | Not touched |
| 10 | T0/T1/T2 versus ladder levels | Assumed: the ladder is canonical (D4). If the captain picks the other, sections 4 and 5 change in words only |
| 11 | `firstmate` mentions in docs | Not touched |
| 12 | Issue #464 | Resolved by the captain: close #464 and link this epic from it, when the epic is filed. #464's literal ask (three rigs that bypass `workflow_worker`, seven hand-built control workers) is issue RIG-W-2, and the rest of #464 is the whole of this epic |

### 0.3 Scout candidates this spec takes, defers or leaves

Numbers are the table rows of section 1.3 of the cleanup scout report, each restated here by its short name.

| Scout rows | Where here |
| --- | --- |
| 1 (cache `naming()` and `pools()`) | WFL-1 |
| 2 (admission and dispatch runtime) | H2, in ADM-3 and DIS-3 |
| 3 (doctor self-test, `DSY_CONTEXT`) | TOOL-2 |
| 7 (one cluster lock) | TOOL-1, TOOL-4 |
| 8 (one logins module) | DB-1 to DB-3 |
| 10, 11, 12 (test rigs and fixtures) | TS-1 to TS-3 and RIG-A, RIG-D, RIG-W, RIG-R |
| 13 (Postgres connect timeout, reaper drain) | the service lanes, H1 and H3 |
| 21 (ready file, signals, config error, Temporal client) | L0-1, L1-1 and the adoption issue of every service lane |
| 23 (generated schema runner) | deferred, not service plumbing |
| 24 (Deployment skeleton helper) | deferred, as the scout said, until a seventh service |
| 4, 5, 6, 9, 14 to 20, 22, 25 | not structure, so not here: CI duration, dead code, docs, dependency hygiene, images, unrun tests. They touch disjoint paths and can run beside this epic, with one exception named in section 13 (row 4 conflicts with `scripts/affected.py`) |

## 1. Goal and non-goals

Goal: break the system into domains from the code, give the plumbing that every service repeats one owner and one test, consolidate the copies behind it one service at a time without ever breaking `main`, and add a smoke level that proves a new workflow module can be added without a core edit.
The cleanup is measured by a copy ledger (section 10, row E-LEDGER) that counts each duplicated pattern before and after.

Non-goals, each with where it goes:

- A framework or a base class for services: the library is functions and small classes, and each service keeps its `Config`, drain steps, readiness meaning, metric names and routes (section 3.3).
- Any contract change, including an open task type set (kind C of Q1): a later epic, with the touch list of section 5.1 as its input.
- CI duration, dead code, dependency hygiene, image contents, docs fixes and unrun test classes: scout groups A, D and E, independent of this epic.
- The Deployment skeleton helper (scout row 24): deferred until a seventh service exists.
- The generated schema runner (scout row 23, P14): deferred, not service plumbing.
- A cluster row for the example module: follow-up after issue #682, under Q3.
- Replacing the 57 `yaml.safe_load` sites or the 59 contract symlinks with a loader framework: the scout rejected it and so does this spec.

## 2. Domains and call patterns

### 2.1 The domain map

How it was derived: the workspace dependency graph, the imports of the five dependency kinds in `packages/*/src`, and the call sites named below.
The map is by ownership of data and of deployables, so a package sits in exactly one domain.

```
 submitters
     |  HTTP (routes.py)
     v
 D1 dispatch --------publish--------> D6 broker --------consume-------> D2 admission
  |  | |  |                                                              |  |  |
  |  | |  +--signal/query/describe--+                       start_workflow|  |  +--claim--+
  |  | +--insert rows/events------+  |                                  v  |             |
  |  +--upload sources--+          |  |                       D3 workflow engine         |
  |                     v          v  v                       (JobWorkflow, control,     |
  |               D5 artifact   D4 state and event store       reaper, emit)             |
  |                  store  <---- (Postgres appdb)  <-----------+   |   |              |
  |                     ^                                       |   |   +--/place-->  D7 placement --> K8s API
  |                     |                                       |   +--pool queues-->  D8 workers and pools
  +---------------------+---------------------------------------+--------------------->(stage files, run guest)
 every service: D10 observability (OTLP, Pyroscope, /metrics), D9 job model and keys (pure libraries)
 host side: D11 tooling, test support and deploy
```

| # | Domain | Packages (src) | Deployables | Owns (data) | In | Out |
| --- | --- | --- | --- | --- | --- | --- |
| D1 | Dispatch and control API | `dsy_dispatchapi` | `dispatch-api` (`deploy/dispatchapi`) | rows of `accepted_job` as writer on accept, the intake publish, source uploads | HTTP from submitters (`service/routes.py`), operators | Postgres as 4 logins (`dispatch`, `control`, `artifacts`, `reader`), RabbitMQ publish (`publish.py:69`), object store as `submit`, Temporal signal, query and describe (`gateway.py:73-88`) |
| D2 | Admission (layer 1) | `dsy_admission` | `admission` (`deploy/admission`) | the `accepted` to `admitted` claim, workflow starts | RabbitMQ intake queues (`intake.py:24,375-381`) | Postgres as login `admission`, Temporal `start_workflow` (`start.py:64`) and queries (`resume.py:180`, `scan.py:186`) |
| D3 | Workflow engine | `dsy_workflow`, `dsy_artifactreaper` (hosted in the same process), `dsy_temporal` | `workflow-worker` (`deploy/workflow`) | `JobWorkflow`, the control activities, status moves and events through `emit` (`activities/emit.py`), the naming and pool contract loaders (`contract.py`) | Temporal workflow and activity tasks, admission's start | Postgres as 3 logins (`workflow`, `artifacts`, `reaper`), object store (`cache`, `commit`, `index`, `plan` activities), placement over HTTP (`placement.py:127,137`), pool task queues |
| D4 | State and event store | `dsy_jobstate`, `dsy_eventstore`, `dsy_artifactindex`, `dsy_stateops`, `dsy_migrate` | none of its own (`appdb`, `deploy/appdb`) | the Postgres schema: tables `accepted_job`, `event`, the artifact index, the 7 group roles of `R__roles.sql` | every writer, as a library on a `psycopg` connection it is handed | Postgres only |
| D5 | Artifact store | `dsy_artifactclient`, `dsy_storecheck` | none (`deploy/objectstore`) | the buckets and their layout, staging, commit, cache and checkpoint rules | D1 (sources), D3, D8, the reaper | the S3 API through `dsy_storecheck.s3` |
| D6 | Broker | `dsy_broker` | none (`deploy/rabbitmq`) | intake queues, dead letters, tenancy profile loader | D1 publish | AMQP 1.0, `proton` imported only in `broker/client.py` |
| D7 | Placement (layer 2) | `dsy_placement` | `placement` (`deploy/placement`) | pool choice, memory class and timeouts, from `pools.yaml` | HTTP `/place` from D3 (`/pools` has no workflow caller) | the Kubernetes API read-only (`kube.py:42-46`) |
| D8 | Workers and pools | `dsy_worker`, `dsy_compile`, `dsy_runtime`, `dsy_aggregate`, `dsy_kernel`, `dsy_workloads` | `compile`, `simulate`, `aggregate` pool workers (`deploy/workers`) | task execution: `run_task`, the guest sandbox | Temporal activity tasks on pool queues | object store through `ArtifactClient`, a child process (wasmtime) |
| D9 | Job model, events and keys | `dsy_jobdoc`, `dsy_jobcheck`, `dsy_plan`, `dsy_jobpatch`, `dsy_jobexamples`, `dsy_jobtools`, `dsy_events`, `dsy_keys`, `dsy_compat`, `dsy_stages` | none | the job document, plan, events, key formulas | every domain | none, pure libraries |
| D10 | Observability | `dsy_obs` | the stack (`deploy/obs/*`) and the `state-inputs` exporter (`obs/inputs/`) | setup of logs, traces, profiles, the metrics runtime, the read client of the stack | every service | OTLP, Pyroscope, a Postgres reader for the exporter |
| D11 | Tooling, test support and deploy | `dsy_itest`, `dsy_clusterkit`, `dsy_acceptance`, test modules inside src packages (`dsy_workflow.testing`, `dsy_workflow.harness`, `dsy_migrate.fixtures`, `dsy_storecheck.fixtures`, `dsy_broker.testing`, `dsy_obs.testing`) | none | `scripts/`, `just/`, `deploy/*`, the host-side cluster drivers | host | the cluster, containers |

After L0-3 the domain map lives in the architecture note `docs/architecture/21-domains-and-access.md`, and this section keeps a link to it.

Three things the map shows that the epic docs do not:

- `naming()` and `pools()` are the Temporal naming contract loaders and live in D3 (`dsy_workflow/contract.py:106,110`), so admission, dispatch, placement, worker and acceptance depend on the workflow package for a YAML read.
  A new library must therefore take the namespace as an argument and not call `naming()` itself, or it would depend on D3 and close a cycle.
- The reaper (`dsy_artifactreaper`, D5 logic) is deployed inside the workflow process (`service/__init__.py:237-241`): its CLI entrypoint `schedule worker` has no deploy unit of its own.
  It is the one existing module that attaches to the workflow host without a core edit being the point, and it is built by hand (section 5.1).
- D4 is already well factored: every table has one owning library that takes a connection, and the role definitions exist once (`packages/stateops/src/dsy_stateops/migrations/R__roles.sql`).
  The only part that is missing is the acquisition of the connection (section 2.3, P1).

### 2.2 Call patterns per deployable

Cells name the construct and the file:line.
"own" means the service builds it by hand.

| | dispatch | admission | workflow (+ control, reaper) | pool worker | placement | state-inputs |
| --- | --- | --- | --- | --- | --- | --- |
| Entry | `service/__init__.py` `main`, 9-line shim | `service/__main__.py:16-32` | `service/__main__.py:15-33` | `__main__.py:18-25` | `service.py:242-279`, 9-line shim | `obs/inputs/__main__.py:38-51` |
| Config | `Config.from_env`, `ConfigError` from `limits.py` | `Config.from_env`, `ConfigError` | `Config.from_env`, `MissingEnv` | `config.load`, `ConfigError` | `Config`, `BadEnv` | inline, print and exit 2 |
| Postgres | 4 DSNs from env, `psycopg.connect` at `retrier.py:114,115,174`, `reconciler.py:77`, `service/__init__.py:235` | 1 DSN, `service/__init__.py:312`, no timeout | 3 DSNs, `activities/db.py:19` (10 s connect, 10 s lock) and `artifactreaper/schedule.py:97` (none) | none | none | 1 DSN, `inputs/exporter.py:55` (5 s) |
| Temporal | `gateway.py:50` own, `lazy`, no runtime | `service/__init__.py:294` own, `lazy`, runtime | `service/__init__.py:208` own, runtime, `describe_namespace`; 3 `Worker(` own (`:229`, `worker.py:37`, `schedule.py:119`) | `service.py:59` own, runtime, `describe_namespace`; `Worker(` at `:77` | none | none |
| Broker | publish (`publish.py`) | consume (`intake.py`) | none | none | none | none |
| Object store | `submit` role: sources, resolver, patch | none | `workflow` and `indexer` roles: cache, commit, plan, index; reaper role | its pool role, staging and upload | none | none |
| Contracts | `models.py:22` `CONTRACT`, jobstate limits, `status.yaml` read in 3 modules, `naming()` | `naming()` 4 times per job start (`start.py:45,65,68,69`), tenancy, status | `contract.py` readers: 3 cached, `naming()` and `pools()` not | `dsy_runtime` classes | `kube.py:19`, `pools()` | none |
| Metrics runtime | own `Runtime` (`service/__init__.py:188`) | own (`service/__init__.py:115-116`) | `dsy_obs.metrics.runtime` (`:157`) | `dsy_obs.metrics.runtime` via `metrics.py` | `dsy_obs.metrics.runtime` (`service.py:267`) | `dsy_obs.metrics.runtime` (`:44`) |
| Ready | HTTP `/readyz`, checks 4 DSNs, store, Temporal | file, optional | file, optional | file, required | file, optional | file, after the first database pass |
| Stop | `signal.signal` + `stop.wait` (`:312`) | `add_signal_handler` (`:291`) | `add_signal_handler` (`:203`) | `add_signal_handler` (`service.py:55`) | `signal.signal` raising `SystemExit` (`:275-276`) | `signal.signal` (`:48`) |
| Drain | 25 s of a 30 s grace | 15 s of 30 s | 60 s of 90 s | 30 s of 75 s | none by design | the pass in hand |

Other places that open a connection or build a client, which the library also serves: the reaper CLI (`artifactreaper/__main__.py:56`, `schedule.py:171,178,188`), `dsy_migrate` (`runner.py:136`), the `dsy_stateops` CLIs (`inputs.py:205`, `invariants.py:65`, `bench.py:281`), the dispatch smoke driver (`smoke/__init__.py:360,446,481`), the workflow harness (`harness/cluster.py:172`) and the acceptance Postgres client (`clients/postgres.py:13`).
That is 19 `psycopg.connect(` lines in `packages/*/src`, of which 10 sit in long-running services and the reaper.

### 2.3 Copies and divergence

"Matters" means the divergence changes behaviour, not only spelling.
Counts were rerun on `main` (section 13).

| # | Pattern | Copies and where | Divergence | Matters | Fixed by |
| --- | --- | --- | --- | --- | --- |
| P1 | Postgres connect | 10 service-side `psycopg.connect(` lines with 4 option sets: no timeout at `admission:312`, `retrier:114,115,174`, `reconciler:77`, `schedule.py:97`, `__main__.py:56`; 5 s at `dispatch:235` and `exporter:55`; 10 s plus a lock timeout at `activities/db.py:19` | an unreachable database blocks the first group until the OS gives up; the dispatch tests work around it with `?connect_timeout=1` in the DSN (`dispatchapi/tests/service/conftest.py:37`) | yes (suspected, not reproduced) | L0-1, then H1 |
| P2 | Temporal client | 4 service-side `Client.connect(` (`admission:294`, `workflow:208`, `worker/service.py:59`, `gateway.py:50`), 3 more with no converter in the reaper CLI (`schedule.py:171,178,188`) | `lazy`, `describe_namespace` and the runtime differ, as the services need; dispatch passes no runtime, so SDK client metrics never reach its `/metrics` | `lazy` and `describe_namespace` are intended; the runtime is a defect (H2) | L1-1, then H2 |
| P3 | Metrics runtime | 5 constructions: shared `dsy_obs.metrics.runtime` in workflow, worker, placement, state-inputs; own `PrometheusConfig(` in admission (`:115`) and dispatch (`:188`) | the own ones skip the catalogue buckets, so `dsy_event_bytes` is most likely exported on default buckets there | yes (verified in code, not on a live `/metrics`) | H2 |
| P4 | Contract read | `naming()` and `pools()` re-read and re-validate the YAML on every call, four times per job start in admission; their three siblings are `@cache`d (`contract.py:100-111`) | none other | yes, a flat cost per job (scout measured 2.4 ms per load) | WFL-1 |
| P5 | Environment and config errors | 5 exception classes (`ConfigError` x3, `MissingEnv`, `BadEnv`), 4 copies of `if missing :=`, 6 print-and-exit-2 blocks, 2 host:port parsers | names and message prefixes; the line is plain stderr while the JSON log handler is already installed | no | L0-1, adoption issues |
| P6 | Ready file | 6 `DSY_READY_FILE` declarations; touched at 5 different moments; removed on stop in 5 of 6 | docstrings claim "once polling"; code touches before the first poll | low | L0-1, adoption issues |
| P7 | Stop on signal | 3 asyncio copies (admission, workflow, worker), 2 `signal.signal` + event copies (dispatch, state-inputs), 1 `SystemExit` (placement) | placement drops in-flight requests by design | no | L0-1, adoption issues |
| P8 | Worker construction | `workflow_worker` is shared (`worker.py:28`), but 3 rigs hand-build it (`admission/tests/scan/conftest.py:326`, `service/conftest.py:288`, `dispatchapi/tests/control/conftest.py:196`) and 7 control workers are hand-built; the reaper worker has no `graceful_shutdown_timeout` (`schedule.py:119`) | rigs lack `max_concurrent_local_activities` and the executor thread prefix, so the production worker's guarantees are not exercised | drift only for rigs; H3 for the reaper | WFL-2, RIG-W-2, H3 |
| P9 | Postgres logins | 4 `deploy/*/logins.py` (668 lines, 144 to 154 identical lines per pair), 4 `logins.sh`, 4 `test_logins.py`, 4 Tilt stanzas | only data differs; a fixed temp name in `secrets_of_logins` can race | security-relevant SQL kept in four places | DB-1 to DB-3 |
| P10 | Namespace variable | worker `DSY_NAMESPACE` (`worker/config.py:14`, `deploy/workers/pool.star:89`), admission and workflow `DSY_TEMPORAL_NAMESPACE`, dispatch none (`naming().namespace`) | one fact under three sources | a misconfiguration trap | H4 |
| P11 | Metrics port | 9090 in dispatch, 9464 in five services | none functional, Prometheus scrapes by port name | no | H5 |
| P12 | Cluster lock | 5 `exec 9>/tmp/dsy-cluster.lock` blocks (`up.sh:29`, `down.sh:9`, `nuke.sh:16`, `cluster-verify.sh:30`, `smoke.sh:22`); only `smoke.sh` exports `DSY_CLUSTER_LOCK_HELD` and exits 4 | one script calling another would wait 900 s on itself; `tilt/run.sh` and `canary.sh` take no lock | latent trap | TOOL-1, TOOL-4 |
| P13 | Test rigs | `"temporalio/temporal"` started by hand in 12 files, 9 `def db(` fixtures, 11 test files that `CREATE ROLE` with 3 teardown strengths, 16 test files that bind their own free port, 8 search-attribute registrations | scope, namespace, persistence and search attributes differ as parameters; teardown strength is drift | the weak teardowns surface a leak differently per package | TS-1 to TS-3, RIG lanes |
| P14 | Generated schema runner | 7 near-identical `gen.py` (about 320 lines) and 4 `test_drift.py` | cosmetic | no | deferred, not service plumbing |

What stays per service (not consolidated, unchanged from the scout's recommendation): the `Config` dataclass and its variable names, drain steps and durations, the meaning of ready, metric names and buckets, whether a service has HTTP routes, and the role-specific credential variable names.

### 2.4 Interfaces

#### Consumes (each pinned to a version)

| From | What | Pinned at | Stub used until it exists |
| --- | --- | --- | --- |
| #17 pass 1 | `just smoke` and the three API scenarios of `python -m dsy_dispatchapi.smoke` | `main` at `ee88fc7` | none |
| issue #682 (#17 pass 2) | a passing `just e2e`, the gate of decision Q5 | the merge of #682 | none, every lane except L0 waits |
| `contracts/` | the naming, pools, status, events and instrumentation contracts, read only | the contracts tree hash of E-CONTRACTS | none |
| `dsy_obs` | `dsy_obs.metrics.runtime(address, buckets)` | `packages/obs/src/dsy_obs/metrics.py:18` | none |
| `dsy_workflow` | `workflow_worker`, `dsy_workflow.testing.connect(address, attempts=...)` | `worker.py:28`, `acceptance/clients/temporal.py:11` | none |
| `dsy_migrate` | the entry-point group `dsy.migrations`, the shape the module seam copies | `runner.py:16,74-77` | none |
| `psycopg`, `temporalio` | the clients every service already uses | the versions in `uv.lock` | none |

#### Produces (each pinned precisely)

| To | What | Where it lives (path) | Shape (schema file, signature, exit codes, key names) | Version |
| --- | --- | --- | --- | --- |
| the six services | `dsy_servicekit` config, lifecycle, pg and testing | `packages/servicekit/src/dsy_servicekit/` | the surface of section 3.2, pinned by the README list and the `__all__` test; exit 2 on a configuration error | the workspace package, from L0-1 |
| the services, rigs | `dsy_temporal.connect` | `packages/temporal/src/dsy_temporal/client.py` | `connect(address, namespace, *, runtime, lazy, check_namespace)` | from L1-1 |
| `deploy/*/logins.py` | `dsy_clusterkit.logins.main` | `packages/clusterkit/src/dsy_clusterkit/logins.py` | the CLI `render`, `apply DSN`, `check DSN` | from DB-1 |
| workflow modules | the entry-point group `dsy.workflow.modules` and `dsy_workflow.modules` | `packages/workflow/src/dsy_workflow/modules.py` | an entry point to `workers(ctx: Context) -> list[Worker]`; `Context(client, build_id, drain)`; `load()`; `__all__` is `Context` and `load` | from MOD-1 |
| cluster scripts | `cluster_lock <exit_code>` | `scripts/cluster/lock.sh` | takes fd 9 on `/tmp/dsy-cluster.lock`, waits `DSY_CLUSTER_LOCK_TIMEOUT` (900 s), exits with `<exit_code>`, exports `DSY_CLUSTER_LOCK_HELD=1` | from TOOL-1 |
| developers, CI | `just seam` | `just/seam.just`, `scripts/seam.sh` | `pytest -m seam`, exit 0 when green | from L0-3 |
| the ladder | level 3a, tier T1 | `docs/acceptance-ladder.md` | the row of section 5.5 | from L0-3 |
| the architecture | the domain map and the seams that remain | `docs/architecture/21-domains-and-access.md` | a note and a draw.io diagram | written in L0-3 |

## 3. The shared access library

### 3.1 What it owns

The rule for placing a piece: it goes to the package that already owns its topic, and only a piece with no owner goes to the one new package.

| Piece | Owner | New or existing | Why there |
| --- | --- | --- | --- |
| Postgres connect with timeouts | `dsy_servicekit.pg` | new | no owner today; `activities/db.py` imports artifact types and so cannot be used by admission |
| Environment reading, the one config error, the exit-2 wrapper, number and host:port parsers | `dsy_servicekit.config` | new | no owner |
| Ready file, stop on signal (asyncio and thread forms) | `dsy_servicekit.lifecycle` | new | no owner |
| Test kit: spawn a service process, `free_port`, `until`, service contract checks | `dsy_servicekit.testing` | new | standard library only, so a unit-only package can use it |
| Temporal client recipe | `dsy_temporal.connect` | existing package, new function | `dsy_temporal` already owns the converter and the tracing interceptor |
| Metrics runtime and registry mirror | `dsy_obs.metrics` | existing, no change | already shared, only two services skip it |
| Host-side Postgres logins | `dsy_clusterkit.logins` | existing package, new module | `dsy_clusterkit` already owns `state_dir()` and `appdb_password()` and the port-forward |
| `control_worker` builder | `dsy_workflow.worker` | existing | beside `workflow_worker` |
| Temporal dev server and rig fixtures, event and plan builders | `dsy_workflow.testing` | existing | already imported by admission, dispatch, workflow and acceptance |
| `db` and `login` fixtures | `dsy_migrate.fixtures` | existing | owns `appdb` |
| `shelf` fixture | `dsy_storecheck.fixtures` | existing | owns the store fixtures |
| OTLP receiver | `dsy_obs.testing` | existing | owns `FakeObs` |
| Cluster lock, state dir, credential, port-forward wait | `scripts/cluster/lock.sh`, `scripts/lib/state.sh`, `scripts/cluster/forward.sh` | shell: `lock.sh` and `state.sh` new, `forward.sh` extended | the shell scripts' own layer |

What it does not own, and why: the contract loaders and the 59 symlinks (they work and are guarded, section 1), the per-table Postgres libraries (already one owner each), the Deployment skeleton (deferred), and the `Config` of any service.

### 3.2 Package layout and public surface

New package `packages/servicekit`, import name `dsy_servicekit`, distribution `dsy-servicekit`.
It depends on `psycopg` only.
It must not import any other `dsy_*` package, and a layering test enforces it (section 4).

```
packages/servicekit/
  pyproject.toml            deps: psycopg ; [tool.uv.sources] empty
  README.md                 the public surface below, with the rule "add here only what two services copy"
  src/dsy_servicekit/
    config.py
    lifecycle.py
    pg.py
    testing.py              stdlib only, no pytest import at module level
  tests/
    test_config.py  test_lifecycle.py  test_pg.py  test_layering.py
    toy/                    a 40-line reference service the kit's contract checks run against
```

Public surface, pinned in the README and by a test that compares `__all__` to a list.
Signatures are a proposal for the lane until decision O2 is answered, and a signature change before lane L0 merges is free.
`connect_timeout` is an `int` of seconds with a floor of 2, because libpq rejects a fractional value.

```python
# dsy_servicekit.config
class ConfigError(Exception): ...
def require(environ: Mapping[str, str], names: Mapping[str, str]) -> dict[str, str]:
    """field -> value. Raises ConfigError("missing environment: A, B") naming every absent or empty variable."""
def positive(environ: Mapping[str, str], name: str, default: float | None = None) -> float: ...
def host_port(value: str, name: str) -> tuple[str, int]: ...
def exiting_on_config_error(component: str):
    """Context manager for `main`: prints "<component>: <error>" to stderr and exits 2, as the six services do today."""

# dsy_servicekit.lifecycle
class ReadyFile:
    def __init__(self, path: Path | None): ...      # None makes every method a no-op
    def arm(self) -> None: ...                      # unlink a stale file
    def set(self) -> None: ...                      # touch
    def clear(self) -> None: ...                    # unlink, missing is fine
@contextmanager
def stopping(loop=None) -> Iterator[asyncio.Event]: ...   # SIGTERM and SIGINT set the event, handlers removed on exit
class ThreadStop:                                          # the signal.signal form of dispatch and state-inputs
    event: threading.Event
    def install(self) -> None: ...

# dsy_servicekit.pg
DEFAULT_CONNECT_TIMEOUT_S = 10
MIN_CONNECT_TIMEOUT_S = 2                       # libpq takes a decimal integer of seconds, and 1 is not reliable
def connect(dsn: str, *, autocommit: bool = False, connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_S,
            lock_timeout: str | None = None, statement_timeout: str | None = None) -> psycopg.Connection: ...

# dsy_servicekit.testing
def free_port() -> int: ...
def until(check, timeout_s: float, what: str, interval_s: float = 0.1): ...   # sync, deadline with a message
class ServiceProcess:
    def wait_ready(self, ready_file: Path, timeout_s: float) -> None: ...
    def terminate(self, timeout_s: float) -> int: ...     # SIGTERM, returns the exit code
def spawn(argv: list[str], env: Mapping[str, str]) -> ServiceProcess: ...
# the three checks below are all the kit offers; the bucket check of a service is its own code over dsy_obs
def check_missing_env_exits_2(argv: list[str], names: Iterable[str]) -> None: ...
def check_ready_file_lifecycle(argv, env, ready_file: Path) -> None: ...
def check_sigterm_exits_0_within(argv, env, ready_file: Path, seconds: float) -> None: ...
```

Additions to existing packages:

```python
# dsy_temporal.connect (packages/temporal/src/dsy_temporal/client.py, new file)
async def connect(address: str, namespace: str, *, runtime: Runtime | None = None,
                  lazy: bool = False, check_namespace: bool = False) -> Client:
    """Client with event_data_converter and tracing_interceptor(), the recipe of the four services.
    `lazy` keeps admission and dispatch able to start with Temporal down;
    `check_namespace` makes workflow and worker fail fast on a wrong namespace."""

# dsy_workflow.worker (beside workflow_worker)
def control_worker(client, build_id: str, activities: list[Callable], **options) -> Worker: ...

# dsy_clusterkit.logins
def main(argv: list[str], *, logins: Mapping[str, tuple[str, str]], namespace: str, state_name: str,
         extra_secrets: ... = None) -> int:
    """render | apply DSN | check DSN, the CLI of the four deploy/*/logins.py files."""
```

`dsy_workflow.testing.connect(address, attempts=...)` is imported by `packages/acceptance/src/dsy_acceptance/clients/temporal.py:11`, so its signature stays.

### 3.3 How it sits beside the contracts and interfaces

- Contracts: untouched (D1).
  The library reads none of them.
  Where it needs a contract value, such as the namespace, the caller passes it.
- Interfaces: each existing name keeps working through a one-line alias (D2), for example `ConfigError = dsy_servicekit.config.ConfigError` in each service module.
  The constraint is that `ENV` and `READY_ENV` stay importable from each service module, and only two package service tests import `MissingEnv` or `BadEnv`.
  Removal of aliases is not part of this epic.
- Dependency direction: `dsy_servicekit` is a leaf.
  `dsy_temporal.connect` takes the runtime as an argument and does not import `dsy_obs`.
  Services depend on `dsy_servicekit`, never the reverse.
- Existing extension precedents are followed, not replaced: entry-point groups `dsy.migrations` (`packages/migrate/src/dsy_migrate/runner.py:16,74-77`) and `pytest11` (`packages/migrate/pyproject.toml:8-9`).
  The module seam of section 5 is a third group of the same shape.
- Affected-package selection: `scripts/affected.py:28-29` reads the dependency graph from each package's `[tool.uv.sources]`, not from `[project] dependencies`, so every adopter must add `dsy-servicekit = { workspace = true }` to its own sources block, or `just pre-pr` will not test it when `dsy_servicekit` changes.
  It must also add `dsy-servicekit` to its `[project] dependencies`, because images are built with `uv export --no-dev --package` (`deploy/admission/image.sh:18`) and a missing dependency passes every test and breaks the pod.
  Both are acceptance criteria of every adoption issue.

### 3.4 Migration order (strangler)

Each step is independently shippable: after any one merged pull request, `main` is green, and the step before it needs no later step to be correct.

| Step | Ships | What changes for a running service | Safety |
| --- | --- | --- | --- |
| S0 | `dsy_servicekit` and `dsy_temporal.connect`, unused (L0, L1) | nothing | the kit's own tests and the toy service |
| S1 | after the gate of Q5, one pathfinder adopts: admission (ADM-1, ADM-2), which has every construct and the most delicate drain | start-up and stop code of one service | existing admission tests plus the kit's contract test; checkpoint CP1, mechanical changes only |
| S2 | the other five services adopt, in parallel (DIS, WFL, WRK, PLC, OBS) | start-up and stop code, one service per pull request | per service: its tests plus the kit's contract test |
| S3 | the behaviour changes H1 to H7, one issue each, each starting from a reproduction, all inside checkpoint CP2 | the named behaviour | a failing test first, then the fix; checkpoint CP2 after the last |
| S4 | test support grows (TS lane, additive), then each package's tests migrate (RIG lanes), each pull request deleting the copies it replaces | tests only | the package's own suite; the copy ledger drops |
| S5 | the module seam, the example module and `just seam` completes (MOD lane) | the workflow service gains one loop over entry points | seam smoke |

Why adoption is per service and not per concern: a concern-by-concern order would edit `admission/service/__init__.py` in five lanes in a row, and a per-service lane edits it once and owns it.
Why admission first: it has the three-way drain (`DRAIN_S = 15.0`, held-delivery release, grace 30 s) that the scout called delicate, so if the helper fits there it fits the others, and only the ready-file and signal lines move, never the drain order.
Revert story: every adoption pull request is a single revert, because aliases mean nothing else imported what it changed.

## 4. Tests

### 4.1 Where each kind of test lives and what it proves

| Test | Lives in | Level (tier) | Proves | Command |
| --- | --- | --- | --- | --- |
| Library unit tests | `packages/servicekit/tests/test_config.py`, `test_lifecycle.py`, `test_pg.py` | 1 (T0) | `require` names every missing variable in one error; `ReadyFile` arms, sets, clears and is a no-op on `None`; `stopping` sets on SIGTERM and removes handlers; `pg.connect` passes `connect_timeout`, `lock_timeout` and `statement_timeout` to `psycopg` | `scripts/test.sh packages/servicekit` |
| Blackhole connect | `packages/servicekit/tests/test_pg.py` | 1 (T0), a local socket that accepts and never answers, no container | `pg.connect` raises within `connect_timeout` plus 1 s (the test passes `connect_timeout=2` and bounds the wait at 3 s), which reproduces the suspected hang of P1 against the old call and passes against the new | same |
| Library integration | `packages/servicekit/tests/test_pg_integration.py`, `packages/temporal/tests/test_client.py` | 2 (T1), marked `integration` and `pair` | `pg.connect` against the real appdb fixture with a lock held elsewhere fails the attempt in `lock_timeout`; `dsy_temporal.connect` against the dev server sends the event data converter and the tracing interceptor, serves its runtime metrics on `/metrics`, and `check_namespace` raises on a wrong namespace | `scripts/itest.sh servicekit`, `scripts/itest.sh temporal` |
| Service contract tests | one file per service, `packages/<service>/tests/service/test_contract.py`, a few lines each over the kit | 1 (T0) for the missing-environment case, 2 (T1) for ready and SIGTERM | all six services behave the same on the shared edges: exit 2 and `missing environment: ...` with an empty environment; the ready file appears only after start-up and is gone on stop; SIGTERM ends with exit 0 inside the Starlark grace period minus 5 s; the kit offers these three checks, dispatch is exempt from the ready-file row (it uses `/readyz`), and the bucket check (the metrics runtime serves the catalogue buckets of `dsy_event_bytes`) is code in each service's own `test_contract.py` over `dsy_obs` | `scripts/test.sh packages/<service>`, `scripts/itest.sh <service>` |
| Layering guard | `packages/servicekit/tests/test_layering.py` (an `ast` scan, no new dependency) | 1 (T0) | `dsy_servicekit` imports no other `dsy_*`; no package under `packages/` other than `exampleflow` imports `dsy_exampleflow`; `exampleflow` imports only names its README lists | `scripts/test.sh packages/servicekit` |
| Logins | `deploy/*/test_logins.py` (kept) plus `packages/clusterkit/tests/test_logins.py` (new, T1) | 1 and 2 | the converged role has the attributes of the scout's list (`nosuperuser nocreaterole nocreatedb noreplication nobypassrls valid until infinity`) and the password in the state file logs in; two concurrent first runs write one password | `scripts/test.sh deploy`, `scripts/itest.sh clusterkit` |
| Lock | `packages/acceptance/tests/test_wrapper.py` extended with the fake `flock` pattern it already uses | 1 | every lock-taking script takes, times out with the exit it is told to, and skips on `DSY_CLUSTER_LOCK_HELD`; two scripts in sequence do not deadlock | `scripts/test.sh packages/acceptance` |
| Seam smoke | section 5 | 3a, tier T1 (containers, no cluster) | the services boot, and a module is added without a core edit | `just seam` |

Why the service contract tests live in each service's package: the kit is a leaf and cannot import six services, and a test package that depends on all six would be a new coupling point.
Each service owns its proof and the library owns the assertions, the same arrangement as `dsy_stages.fakes` and `dsy_broker.testing`.

### 4.2 How they run in the existing tiers

- Level 1 runs in `just test` and in every shard (`scripts/ci-shards.sh` puts a new package in the last shard unless it is named, so `servicekit` needs no edit and may be named later for balance).
- Level 2 runs in `just itest <domain>` and in every shard.
  The library's integration tests need a database and a Temporal server in one session, so they carry `pair`, and the T1 session then holds two containers and both host-wide lock files (`packages/itest/src/dsy_itest/session.py:31,61-63`).
  That serialises integration runs across worktrees, which is why the parallelism of section 11 is capped.
- `just pre-pr` runs the unit tests of every dependent of a changed package only when its `[tool.uv.sources]` is right (section 3.3), so a change to `dsy_servicekit` tests all six services.
- One new marker, `seam`, registered once in the root `pyproject.toml` beside `integration` and `slow`, selects the seam smoke: `just seam` is `pytest -m seam`.
  L0-3 registers it, and turning `--strict-markers` on is RIG-R-3, which also moves the `pair`, `isolated` and `cluster` registrations to one place (scout area 2, F16).
- The contract tests of `contracts/` and the generated-schema tests are not changed.
  The check that they stay unchanged is the contracts tree hash of section 10.

### 4.3 What stays untested here, and why

- The real SIGTERM drain of a pod under `terminationGracePeriodSeconds`: only the cluster shows the kubelet's behaviour, so the contract test checks the Python side (exit 0 inside grace minus 5 s) and `just smoke` plus the `restart` scenario cover the rest.
- A Postgres failover: the blackhole test shows the bounded connect, not the retry policy of each caller.
  H1 reviews the retry path per site and records the result in its pull request.
- Metrics on a live scrape: the contract test compares the runtime's configured buckets, not a scraped `/metrics`.
  The live check (`docs/runbooks/drills/live-check.md`) stays the proof, and the worker of checkpoint CP2 runs it for ADM-3 and DIS-3 under the cluster lock.

## 5. The seam smoke level

### 5.1 What exists, and what a module would touch

Today the only code that adds a worker to the workflow host is written into `serve`:
three workers are built by hand, the job worker through `workflow_worker`, the control worker through a literal `Worker(`, and the reaper through `schedule.build_worker` (`packages/workflow/src/dsy_workflow/service/__init__.py:219-241`).
The reaper is already a module in everything but name: it has its own task queue constant (`schedule.TASK_QUEUE`) outside the naming contract, its own thread pool and its own package.

The repo already has one working extension seam of the shape needed: packages register database migrations through the entry-point group `dsy.migrations`, and `dsy_migrate.runner.discover` loads them (`runner.py:16,74-77`).
Four packages use it (`artifactindex`, `eventstore`, `jobstate` and `stateops`), plus a test fixture.

The touch list by kind of addition (Q1), counted on `main`:

| Kind | Core paths a new one must edit today | Count |
| --- | --- | --- |
| A job template | none | 0 |
| B workload | none (`dsy_workloads` is a bundle; a new workload needs a case in `main.py`, which is the library of workloads and not the core) | 0 |
| C task type | the literal `("compile", "simulate", "aggregate")` and per-type branches in `jobdoc/loader.py:18,179,208`, `jobdoc/profile.py:13`, `jobdoc/registry.py:15-25`, `jobdoc/models.py:284-296`, `jobdoc/plan_models.py:42-55`, `jobcheck/{limits,references,resolver,resources}.py`, `jobpatch/check.py:16-18`, `plan/{binding,stats}.py`, `placement/metrics.py:12`, `dispatchapi/submit.py:392-394`, `packages/events/src/dsy_events/models.py:213`, `packages/workflow/src/dsy_workflow/models.py:54`, `packages/workflow/src/dsy_workflow/contract.py:38-39`, plus `contracts/job/registry.json`, `contracts/temporal/pools.yaml`, a pool Deployment | 17 source files and 3 contract or deploy files |
| D workflow module | `dsy_workflow.service.serve` (hand-written worker list) | 1 file, and no core edit after the seam exists |

This table is the pinned touch list of kind C, and L0-3 copies it into the architecture note's "seams that remain", where the later epic that opens the task type set finds it.

### 5.2 The seam

A new entry-point group, `dsy.workflow.modules`, with the shape of `dsy.migrations`, whose entry point resolves directly to a function:

```toml
# packages/exampleflow/pyproject.toml
[project.entry-points."dsy.workflow.modules"]
exampleflow = "dsy_exampleflow:workers"
```

```python
# packages/workflow/src/dsy_workflow/modules.py (new, lane MOD); __all__ = ["Context", "load"]
@dataclass(frozen=True)
class Context:
    client: Client          # converter, tracing interceptor and runtime already applied (dsy_temporal.connect)
    build_id: str
    drain: timedelta        # the host's DRAIN; a module passes it as graceful_shutdown_timeout of its Worker

def load() -> list[tuple[str, Callable[[Context], list[Worker]]]]: ...
# entry points of the group sorted by name; an entry point whose import fails, or whose name repeats, is skipped and logged at error level
```

The host change is one loop in `serve`: for each `(name, workers)` of `modules.load()`, `workers(ctx)` is called, with a construction error skipped and logged at error level with the module name.
Each module worker runs under its own supervisor task in the same loop, outside `_run_until`.
What is frozen is the supervision and fail-fast logic of `_run_until` for the three built-in workers: the `FIRST_COMPLETED` wait, the shutdown and the gather.
Its ready-file touch and unlink lines and the type of its `ready_file` parameter move to `ReadyFile` in WFL-1, and nothing else in it changes.
A supervisor logs the failure of its module worker at error level and does not stop the host.
At SIGTERM the host shuts the module workers down together with the built-in ones, and the drain holds because the module builds its `Worker` with `graceful_shutdown_timeout=ctx.drain`.
A module that needs a thread pool for synchronous activities owns it and closes it itself, as the reaper does, and never uses the host's executor.
A module owns its task queue name, as the reaper does, and imports only the names `dsy_workflow.modules.__all__` and its own README list.

A bad module never keeps the host from serving jobs: a module that raises on import, in construction or on a repeated name is skipped and logged at error level, the host starts without it, and a module worker that dies later does not end the host.
The three built-in workers keep their fail-fast behaviour under the frozen supervision logic of `_run_until`, and they stay as they are: moving them into modules is not needed for the seam and would touch the most delicate code of the service.
This is a design choice flagged for the captain's review, and it is listed in the architecture note with the other seams that remain.

### 5.3 The worked example

`packages/exampleflow` (import `dsy_exampleflow`), about 60 lines:

- `EchoWorkflow.run(text: str) -> {"text": str, "length": int}` calling one activity `echo` on the queue `dsy-exampleflow`.
- `workers(ctx: Context) -> list[Worker]`, the entry point target, returning one `Worker` on `ctx.client` with `graceful_shutdown_timeout=ctx.drain`.
- No database table, no event type and no metric of its own, because each of those touches a core contract: the event catalogue (`contracts/events`), the roles file (`R__roles.sql`) and the instrumentation catalogue (`contracts/instrumentation/metrics.yaml`, held by `packages/obs/tests/test_catalogue_names.py`).
  They are listed in the architecture note as the seams that remain, together with the bad module rule of section 5.2, so a module that needs one opens a contract change on purpose.
- A second and third example, at no core cost, in the same package's README: kind A is one job document in `packages/jobexamples`, kind B is one workload case in `dsy_workloads` run through the existing smoke job path.

### 5.4 What the level proves, and what it costs

`just seam` is `pytest -m seam`, with these test groups:

| Group | Proves | Where |
| --- | --- | --- |
| Boot | each of the six services starts against real containers, writes its ready file, answers its probe, and drains on SIGTERM with exit 0 and no log line at error level | the existing `tests/service/` suites of admission, dispatch, workflow, placement and worker, marked `seam` by ADM-1, DIS-1, WFL-1, WRK-1 and PLC-1, plus the state-inputs exporter's test, marked by OBS-1, and the toy service's boot test, marked by L0-3; reuses `dsy_servicekit.testing.spawn` in place of the 5 hand-written spawn fixtures |
| Contract | the library edges of section 4.1 hold for all six | each service's `test_contract.py`, marked `seam` by the adoption issue that creates it, and the three `check_*` tests of L0-2 against the toy, marked by L0-3 |
| Example module | the real example package, which nobody wrote into the workflow package, is discovered, built on the host's client and served beside `JobWorkflow`: `serve` with `exampleflow` installed completes an `EchoWorkflow` and a level 0 job in the same process; it installs from its wheel closure into a clean virtualenv with the workflow wheel and is found by entry point | `packages/exampleflow/tests/`, marked `seam` by MOD-2, using the `test_wheel.py` pattern of the 22 existing wheel tests |
| Layering | no core package imports a module, and a module imports only its public list | `packages/servicekit/tests/test_layering.py`, marked `seam` by L0-1 |

`scripts/seam.sh` prints one summary line per group: it runs `pytest -m seam` once per group over the group's path list, times each run, and prints the total.
The path lists are the boot suites (the `tests/service/` directories except the `test_contract.py` files, the state-inputs exporter's test and the toy's boot test), the contract files (with the toy's `check_*` tests), `packages/exampleflow/tests` and `packages/servicekit/tests/test_layering.py`.

The zero-touch claim is proven in two places that do not depend on each other:
at review time, the example's pull request (issue MOD-2) must show `git diff --name-only origin/main...HEAD` limited to `packages/exampleflow/`, two lines in the root `pyproject.toml` (the workspace source and the dev group) and `uv.lock`;
and forever after, the exampleflow test serves the host with a module from another package, which cannot pass if `packages/workflow` has to name it.
The no-module path stays proven too: MOD-1 runs the existing service tests with the entry-point group hidden, because once `exampleflow` is in the dev group those tests would otherwise always see a module.

Budget: not measured, so the ladder row carries a proposal of 5 minutes that issue MOD-3 replaces with the first measured run, as the smoke budget was set (ladder section "Scenarios of level 4").
The level needs Docker and no cluster, so it takes the T1 lock and not the cluster lock.

### 5.5 How it relates to the acceptance ladder

- It is a new level, 3a, between integration and the cluster smoke, because it is a gate in the sense of rule 3 (fast, platform-level, no behaviour variants) but does not need the cluster.
  Rule 3 says pass 2 adds no rows to the smoke gate, and this level adds none either: `just smoke` is unchanged.
- It answers a different question from level 4.
  The e2e proves the platform works on public surfaces for a job.
  Level 3a proves the platform stays extensible and its services keep one shape.
  Rule 2 applies: a failure that only level 3a catches means a missing lower-level test, and the fix adds one.
- Gate order becomes: `just ci` on the change, then `just seam`, then `just up` and module deploy, `just cluster-verify`, `just smoke`, then `just e2e` (rule 5, "a level only runs when the level below it is green").
- Ladder document edit (issue L0-3), kept to one table row and one short section, and rebased over pass 2's edits (shared-file rule, section 6): the row

  `| 3a Seam smoke (tier T1) | Every service boots and drains on the shared start-up code, and a new workflow module is added without a core edit | This epic | After every change to a service entrypoint, to \`dsy_servicekit\`, to \`dsy_workflow.service\` or to a module, and before \`just smoke\` | \`just seam\`, 5 minutes proposed, containers and no cluster | delivered by this epic |`

- Relation to the full-system proof: level 3a does not replace `just smoke`.
  The two cluster checkpoints CP1 and CP2 of section 3.4 are the full-system proof during the epic, together with the e2e that issue #682 delivers before any of them runs.

## 6. Lanes

One worker per lane.
Paths are exclusive, and a path two lanes need is a shared file with a rule below the table.
Globs are explicit so that two lanes never own the same directory: a service lane owns named files of its package, the new test files that lane adds under its package's `tests/`, and that package's `pyproject.toml` for the one dependency line.
A RIG lane owns only the existing test files it migrates.

| Lane | Owns (paths, exclusive) | Issues | Needs | Workers |
| --- | --- | --- | --- | --- |
| L0 library | `packages/servicekit/**`, `just/seam.just`, `justfile` (the one `import 'just/seam.just'` line only), the root `pyproject.toml` (the `dsy-servicekit` workspace source and dev group lines and the `seam` marker line only), `scripts/seam.sh`, `docs/architecture/21-domains-and-access.*` | L0-1, L0-2, L0-3 | none, the barrier; may start before the e2e gate | 1 |
| L1 client recipe | `packages/temporal/src/dsy_temporal/client.py`, `packages/temporal/tests/test_client.py` | L1-1 | the e2e gate (Q5) only: its paths are disjoint from L0 and nothing imports it before ADM-2 | 1 |
| ADM (pathfinder) | `packages/admission/src/dsy_admission/service/**`, `packages/admission/pyproject.toml`, new tests under `packages/admission/tests/service/`, `deploy/admission/{Tiltfile.module,README.md,test_render.py}` | ADM-1, ADM-2, ADM-3, ADM-4 | L0, L1 | 1 |
| DIS | `packages/dispatchapi/src/dsy_dispatchapi/{service/**,gateway.py,retrier.py,reconciler.py,limits.py}`, `packages/dispatchapi/pyproject.toml`, new tests under `packages/dispatchapi/tests/service/`, `deploy/dispatchapi/{Tiltfile.module,README.md,test_render.py}` | DIS-1, DIS-2, DIS-3, DIS-4 | CP1 merged | 1 |
| WFL | `packages/workflow/src/dsy_workflow/{service/**,worker.py,contract.py,activities/db.py}`, `packages/workflow/pyproject.toml`, `packages/artifactreaper/src/dsy_artifactreaper/{schedule.py,__main__.py}`, `packages/artifactreaper/pyproject.toml`, new tests under `packages/workflow/tests/service/`, `deploy/workflow/{Tiltfile.module,README.md,test_render.py}` | WFL-1, WFL-2, WFL-3 | CP1 merged | 1 |
| WRK | `packages/worker/src/dsy_worker/{service.py,config.py,__main__.py,metrics.py}`, `packages/worker/pyproject.toml`, new tests under `packages/worker/tests/service/`, `deploy/workers/{pool.star,test_render.py}` | WRK-1, WRK-2 | CP1 merged | 1 |
| PLC | `packages/placement/src/dsy_placement/{service.py,__main__.py}`, `packages/placement/pyproject.toml`, new tests under `packages/placement/tests/service/`, `deploy/placement/{Tiltfile.module,test_render.py}` | PLC-1 | CP1 merged | 1 |
| OBS | `packages/obs/src/dsy_obs/inputs/**`, `packages/obs/pyproject.toml`, new tests under `packages/obs/tests/inputs/`, `deploy/obs/state-inputs/{Tiltfile.module,test_render.py}` | OBS-1, OBS-2 | CP1 merged | 1 |
| DB logins | `packages/clusterkit/src/dsy_clusterkit/logins.py`, `packages/clusterkit/tests/test_logins.py`, `deploy/{admission,dispatchapi,workflow,obs/state-inputs}/{logins.py,logins.sh,test_logins.py}`, `scripts/storage/db/logins.sh` | DB-1, DB-2, DB-3 | L0 | 1 |
| TOOL scripts | `scripts/cluster/{lock.sh,forward.sh,allocatable.sh,canary.sh,namespaces.sh,obs.sh}`, `scripts/lib/state.sh`, the lock blocks and state lines of `scripts/{up,down,nuke,cluster-verify,smoke}.sh`, `scripts/tilt/run.sh`, `scripts/storage/db/{secret,secrets}.sh`, `scripts/storage/store/identities.sh`, `deploy/obs/grafana/secrets.sh`, `docs/runbooks/operator.md` | TOOL-1, TOOL-2, TOOL-3, TOOL-4 | the e2e gate (Q5) only: shell paths disjoint from L0, and no consumer | 1 |
| TS test support | `packages/migrate/src/dsy_migrate/fixtures.py`, `packages/storecheck/src/dsy_storecheck/fixtures.py`, `packages/broker/src/dsy_broker/testing/**`, `packages/obs/src/dsy_obs/testing/**`, `packages/workflow/src/dsy_workflow/{testing,harness}/**`, `packages/clusterkit/src/dsy_clusterkit/__init__.py`, `packages/clusterkit/pyproject.toml` | TS-1, TS-2, TS-3 | L0 | 1 |
| RIG-A | the existing files of `packages/admission/tests/**` that it migrates, except `tests/service/test_contract.py` | RIG-A-1 | TS, ADM | 1 |
| RIG-D | the existing files of `packages/dispatchapi/tests/**` that it migrates, except `tests/service/test_contract.py` | RIG-D-1 | TS, DIS | 1 |
| RIG-W | the existing files of `packages/workflow/tests/**` except `tests/service/test_contract.py` and `tests/test_modules.py`, of `packages/temporal/tests/**` except `test_client.py`, and of `packages/worker/tests/**` except `tests/service/test_contract.py` | RIG-W-1, RIG-W-2 | TS, WFL, WRK | 1 |
| RIG-R | the existing `tests/` files of `artifactclient`, `artifactreaper`, `stateops`, `jobstate`, `eventstore`, `artifactindex`, `broker`, `obs`, `placement`, `storecheck`, `aggregate`, `compile`, `workloads`, `runtime`, `acceptance` (except `test_wrapper.py`) that it migrates, `deploy/conftest.py`, root `pyproject.toml` marker block | RIG-R-1, RIG-R-2, RIG-R-3 | TS, PLC, OBS | 1 |
| MOD module seam | `packages/workflow/src/dsy_workflow/modules.py`, `packages/workflow/tests/test_modules.py`, `packages/exampleflow/**` | MOD-1, MOD-2, MOD-3 | L0, WFL, TS | 1 |
| EXIT | `docs/delivery/epics/19-platform-seams.md` Closure | EXIT-1 | all | 1 |

Gate rule (decision Q5): only L0 and its own tests may start before the e2e of issue #682 lands.
Every other lane starts after `just e2e` runs a real suite and passes on `main`, because the e2e is part of the regression net of the refactor.
The departure from `docs/delivery/method.md:7`, which starts the other lanes only after lane 0 has merged, is stated for L1 and TOOL in section 8.1.

Shared-file rules:

- `pyproject.toml` (root, dev dependency group and marker block), `uv.lock`: the hotspots of method section "Lane isolation".
  A lock conflict is never resolved by hand, `uv lock` is rerun after rebase, and a dependency lands in the pull request that needs it.
  Each adopter adds only its own two lines to its package's `pyproject.toml`: `dsy-servicekit` under `[project] dependencies` and `dsy-servicekit = { workspace = true }` under `[tool.uv.sources]`.
  The root `pyproject.toml` takes two lines from L0-1 (the `dsy-servicekit` workspace source and dev group entry), the `seam` marker line from L0-3, two lines from MOD-2 (the `exampleflow` workspace source and dev group entry), and the marker block of `pair`, `isolated` and `cluster` and `--strict-markers` from RIG-R-3.
- Marking tests `seam`: each adoption issue marks its package's existing `tests/service/` boot suite, the one edit a service lane makes to an existing test file there, and the RIG lanes migrate those files afterwards; every other `seam` mark is made by the issue that creates the test (section 5.4).
- `justfile` (root): L0-3 adds the one `import 'just/seam.just'` line and nothing else.
- `docs/acceptance-ladder.md`: this epic owns the row 3a and a section "Seams"; #17 pass 2 owns every e2e row and the direct-reads table.
  Whoever lands second rebases.
- `scripts/ci.sh`: TOOL-2 adds one line (the doctor self-test), and the CI duration workers own the rest.
- `packages/acceptance/tests/test_wrapper.py`: TOOL-1 extends it, and #17 pass 2 may touch it.
  Same rule as above.
- `packages/workflow/src/dsy_workflow/service/__init__.py` is owned by WFL until WFL merges, then by MOD for the one loop (MOD-1 needs WFL).
- A service or other lane may add its own new test files under its package's `tests/`, and a RIG lane owns only the existing files it migrates, only after the service lane has merged, so the two never edit the same file in the same period.
- MOD owns `packages/workflow/tests/test_modules.py`, which is carved out of RIG-W.
- An out-of-lane edit is a review rejection (method, lane isolation rule 6).

## 7. Issues

Format: each block lists scope, owned paths (by lane, section 6), acceptance criteria that name a command and an observable result, a verifier, what it consumes and produces, `needs`, size and kind.
Sizes: XS under 30 diff lines, S under 150, M 150 to 600, L above 600.
Kind: mech means one worker can do it with the existing tests as the safety net and no judgement, and judge means a per-site decision is part of the work.
Every issue's "done" is the lane issue template's: `just pre-pr` green or `just ci` when it asks for it, the CI `check` job green, pasted output of the named commands, and nothing outside the owned paths changed.
Each issue also carries the labels `epic:N`, `lane:<x>` and the tier the template asks for.
The gate sentence "Gate: decision Q5 (only L0 starts before the e2e gate)." is carried verbatim by the L0, L1 and TOOL blocks, which are the lanes that have no other need.
Every other issue trails one of them through its needs.
After approval these blocks are replaced by `#NNN` links.

### Lane L0: the library

**L0-1 `dsy_servicekit` core: config, lifecycle, pg** - M, judge (the public surface)
- Scope: the package of section 3.2 without the test kit, with the toy service of `tests/toy/`, and the two root `pyproject.toml` lines that make it a workspace member (`dsy-servicekit = { workspace = true }` under `[tool.uv.sources]` and the entry in the dev dependency group); the layering test is marked `seam`.
- Criteria: `scripts/test.sh packages/servicekit` passes with no skip and covers each public name; `pg.connect` with `connect_timeout=2` against a blackhole socket raises inside 3 s, and a value below 2 is raised to 2; the package has no import of another `dsy_*` (layering test); `__all__` equals the list in the README.
- Verifier: `packages/servicekit/tests/`, and `uv lock --check`.
- Consumes / produces: consumes `psycopg`; produces `dsy_servicekit.config`, `dsy_servicekit.lifecycle` and `dsy_servicekit.pg`.
- Needs: none.
  Blocks every issue that needs L0.
- Gate: decision Q5 (only L0 starts before the e2e gate).

**L0-2 `dsy_servicekit.testing`: spawn, free_port, until, contract checks** - M, judge
- Scope: the kit of section 3.2, with `check_missing_env_exits_2`, `check_ready_file_lifecycle` and `check_sigterm_exits_0_within` run against the toy service.
- Criteria: each check fails against a deliberately broken toy (exit 1 instead of 2, ready file left behind, SIGTERM ignored) and passes against the good one, shown by three tests; `testing` imports nothing outside the standard library at module level.
- Verifier: `scripts/test.sh packages/servicekit`.
- Consumes / produces: consumes L0-1; produces `dsy_servicekit.testing`.
- Needs: L0-1.
- Gate: decision Q5 (only L0 starts before the e2e gate).

**L0-3 The `seam` marker, `just seam`, ladder level 3a, architecture note** - S, mech
- Scope: register marker `seam`; `just/seam.just` with `seam` forwarding to `scripts/seam.sh` (`pytest -m seam`, budget timer, exit 5 as success like `scripts/itest.sh`) and the one added `import 'just/seam.just'` line in the root `justfile`; `scripts/seam.sh` runs `pytest -m seam` once per group over the path lists of section 5.4 and prints one summary line per group and the total seconds; it marks the toy's boot test and the three `check_*` tests of L0-2 `seam` and names them in the pull request; the ladder row and section; `docs/architecture/21-domains-and-access.md` with the domain map of section 2.1, its draw.io diagram, and the kind C touch list of section 5.1 copied into its "seams that remain".
- Criteria: `nix develop -c just seam` exits 0, runs the toy's seam tests and prints one summary line per group that has tests, with the total seconds; `scripts/check-links.sh` and `scripts/export-diagrams.sh --check` clean; `scripts/check-contracts-map.sh` unchanged; `git diff --stat origin/main -- contracts/` empty.
- Verifier: the commands above.
- Consumes / produces: consumes L0-2; produces `just seam`, the marker `seam`, ladder row 3a and the architecture note.
- Needs: L0-2.
- Gate: decision Q5 (only L0 starts before the e2e gate).

### Lane L1: the client recipe

**L1-1 `dsy_temporal.connect`** - S, mech
- Scope: the function of section 3.2 with `runtime`, `lazy` and `check_namespace`.
- Criteria: `scripts/itest.sh temporal` shows against a dev server that the client carries `event_data_converter` and the tracing interceptor, that SDK request metrics appear on the passed runtime's `/metrics`, and that `check_namespace=True` raises for an unknown namespace; a unit test shows `lazy=True` returns without a server.
- Verifier: `packages/temporal/tests/test_client.py`.
- Consumes / produces: consumes the Temporal SDK client and runtime; produces `dsy_temporal.connect`.
- Needs: none.
- Gate: decision Q5 (only L0 starts before the e2e gate).

### Lane ADM: admission, the pathfinder

**ADM-1 Admission adopts the config and lifecycle helpers** - S, mech
- Scope: `ConfigError` becomes the alias of the kit's; `if missing :=` becomes `require`; the exit-2 block becomes `exiting_on_config_error`; `READY_ENV` handling becomes `ReadyFile`; the asyncio signal block becomes `stopping`; `dsy-servicekit` is added to `packages/admission/pyproject.toml` under `[project] dependencies` and `[tool.uv.sources]`; its service boot test (`tests/service/`, for obs the exporter's test) and the `test_contract.py` it creates are marked `seam`.
  The drain, `DRAIN_S`, the held-delivery release and the order of `_run`'s `finally` do not change.
- Criteria: `scripts/test.sh packages/admission` and `scripts/itest.sh admission` pass unchanged; `grep -rn 'if missing :=' packages/admission/src` and `grep -rn add_signal_handler packages/admission/src` print nothing; the new `tests/service/test_contract.py` passes (exit 2, ready file lifecycle, SIGTERM exit 0 under 25 s); `pytest deploy/admission -q` passes unmodified; `uv export --frozen --no-dev --package dsy-admission | grep servicekit` prints a line, because images are built from that export.
- Verifier: the commands above; `scripts/affected.py` lists `admission` for a change to `packages/servicekit/`.
- Consumes / produces: consumes `dsy_servicekit.config` and `dsy_servicekit.lifecycle`; produces `packages/admission/tests/service/test_contract.py`.
- Needs: L0, L1.

**ADM-2 Admission builds its Temporal client through `dsy_temporal.connect`** - S, mech
- Scope: `service/__init__.py:294-301` becomes `connect(address, namespace, runtime=runtime, lazy=True)`.
- Criteria: existing service tests pass; `grep -rn 'Client.connect(' packages/admission/src` prints nothing.
- Verifier: `scripts/itest.sh admission`.
- Consumes / produces: consumes `dsy_temporal.connect`; produces nothing new.
- Needs: ADM-1.
  CP1 follows this issue.

**ADM-3 Admission and the metrics runtime (H2)** - S, judge (Q4)
- Scope: replace `prometheus()` at `service/__init__.py:114-116` by `dsy_obs.metrics.runtime(address, BUCKETS)`, keeping the injection point; add the per-service bucket check to `tests/service/test_contract.py`.
- Criteria: first a failing test shows the current runtime's `histogram_bucket_overrides` lacks `dsy_event_bytes`, then it passes, with the expected bounds read from `contracts/instrumentation/metrics.yaml` and not copied into the test; the live scrape is run by the CP2 worker under the cluster lock, and the scraped `dsy_event_bytes_bucket` bounds must equal the values in that file, the output pasted in the CP2 record.
- Verifier: `packages/admission/tests/service/test_obs.py` (new test), and the CP2 record.
- Consumes / produces: consumes `dsy_obs.metrics.runtime`; produces the corrected bucket bounds on admission's `/metrics`.
- Needs: ADM-2, and CP1 merged.
  Lands before CP2.

**ADM-4 Admission Postgres connect with a timeout (H1)** - S, judge (Q4)
- Scope: `service/__init__.py:312` uses `pg.connect`; each caller of the handle (`intake.py:397,416`, `service/__init__.py:251`) is reviewed for what a connect error does there.
- Criteria: a test that points the DSN at a blackhole socket with `connect_timeout=2` shows the intake cycle reporting the database unavailable inside 3 s and the loop continuing, failing first against the unbounded call; the review of each caller is in the pull request.
- Verifier: `packages/admission/tests/service/` new test.
- Consumes / produces: consumes `dsy_servicekit.pg`; produces a bounded connect in admission.
- Needs: ADM-1, and CP1 merged, so the pathfinder stays mechanical.
  Lands before CP2.

### Lane DIS: dispatch

**DIS-1 Dispatch adopts the config and lifecycle helpers** - S, mech
- Scope: `ConfigError` (defined in `limits.py:19`) aliases the kit's; `require` replaces `if missing :=` (`:129`); `ThreadStop` replaces `signal.signal` at `:312`; no ready file (dispatch uses `/readyz`); `dsy-servicekit` is added to `packages/dispatchapi/pyproject.toml` under `[project] dependencies` and `[tool.uv.sources]`; its service boot test (`tests/service/`, for obs the exporter's test) and the `test_contract.py` it creates are marked `seam`.
- Criteria: as ADM-1 for dispatch, including the `uv export` line; `tests/service/test_contract.py` checks exit 2 and SIGTERM exit 0 under 25 s and does not check a ready file.
- Verifier: `scripts/test.sh packages/dispatchapi`, `scripts/itest.sh dispatchapi`, `pytest deploy/dispatchapi -q`.
- Consumes / produces: consumes `dsy_servicekit.config` and `dsy_servicekit.lifecycle`; produces `packages/dispatchapi/tests/service/test_contract.py`.
- Needs: CP1 merged.

**DIS-2 Dispatch builds its Temporal client through `dsy_temporal.connect`** - S, mech
- Scope: `gateway.py:50-57` becomes `connect(address, naming().namespace, lazy=True)` with no runtime, as today; the gateway's private event loop thread (`gateway.py:44-47`) is kept.
- Criteria: `grep -rn 'Client.connect(' packages/dispatchapi/src/dsy_dispatchapi/gateway.py` prints nothing; gateway tests pass.
- Verifier: `scripts/itest.sh dispatchapi`.
- Consumes / produces: consumes `dsy_temporal.connect`; produces nothing new.
- Needs: DIS-1.

**DIS-3 Dispatch metrics runtime and client runtime (H2)** - S, judge (Q4)
- Scope: `build()` creates the runtime with `dsy_obs.metrics.runtime(address, BUCKETS)`, which `Service.__init__` builds by hand at `service/__init__.py:188-194` today, and passes it to both `TemporalGateway` and `Service`; `service.runtime` stays an attribute for `main` and the obs tests; `Service` still builds its own runtime when none is passed, so the tests that construct it with a fake gateway need no edit.
- Criteria: a failing test first shows no `temporal_request` series on dispatch's `/metrics` and the missing bucket override, then passes; the live scrape is run by the CP2 worker under the cluster lock and pasted in the CP2 record.
- Verifier: `packages/dispatchapi/tests/service/test_obs.py`, and the CP2 record.
- Consumes / produces: consumes `dsy_obs.metrics.runtime`; produces SDK request metrics and the catalogue buckets on dispatch's `/metrics`.
- Needs: DIS-2.
  Lands before CP2.

**DIS-4 Dispatch Postgres connect with a timeout (H1) and the 9090 rider (H5)** - S, judge (Q4)
- Scope: the five connect sites (`retrier.py:114,115,174`, `reconciler.py:77`, `service/__init__.py:235`) use `pg.connect`, the readiness one keeping its 5 s; H5 moves the port in `deploy/dispatchapi/Tiltfile.module:67,103` and its README, and drops the redundant `load_dynamic` guard (`:70-71`) only if no runbook names 9090 for dispatch.
  The closed epics `docs/delivery/epics/13-dispatch-api.md` and `docs/delivery/epics/18-observability.md` name 9090 as history and are left alone, or get a one-line note.
- Criteria: the blackhole test first fails then passes for the retrier and the reconciler; the flaky `test_request_during_outage_delivered_after` (scout, section 1.5) is run 20 times before and after with the counts pasted; `grep -rnw 9090 deploy/dispatchapi docs/runbooks | grep -v '19090:9090'` is empty after H5 (the `19090:9090` Prometheus forward lines in `docs/runbooks/alerts.md` and `docs/runbooks/drills/prometheus-down.md` are another service, stay, and are filtered out; `-w` alone would match their `:9090` half).
- Verifier: `scripts/itest.sh dispatchapi`, `pytest deploy/dispatchapi -q`.
- Consumes / produces: consumes `dsy_servicekit.pg`; produces bounded connects and the 9464 metrics port in dispatch.
- Needs: DIS-1.
  Lands before CP2.

### Lane WFL: workflow service, reaper

WFL has no H2 item, because the workflow service already uses `dsy_obs.metrics.runtime`.

**WFL-1 Cache `naming()` and `pools()`; the workflow service adopts config and lifecycle** - S, mech
- Scope: `@cache` on `contract.py:106,110`; `MissingEnv` aliases the kit's error and `require` replaces the check (`:118`); `ReadyFile` and `stopping` replace the file and signal lines (`:198-199,257-263`, `:202-203,250-251`); the drain constant and the supervision and fail-fast logic of `_run_until` (`FIRST_COMPLETED` wait, shutdown, gather) do not change, so the built-in workers stay fail-fast, while its ready-file touch and unlink lines and the `ready_file` parameter type move to `ReadyFile`; `dsy-servicekit` is added to `packages/workflow/pyproject.toml` under `[project] dependencies` and `[tool.uv.sources]`; its service boot test (`tests/service/`, for obs the exporter's test) and the `test_contract.py` it creates are marked `seam`.
- Criteria: a new test shows two calls to `naming()` return the same object and read the file once (monkeypatch `_read`); the existing suites pass (`scripts/test.sh packages/workflow`, `scripts/itest.sh workflow`), and the tests that rewrite `naming.yaml` are listed in the pull request with what each does about the cache (scout: check first); `tests/service/test_contract.py` passes; `uv export --frozen --no-dev --package dsy-workflow | grep servicekit` prints a line.
- Verifier: the commands above.
- Consumes / produces: consumes `dsy_servicekit.config` and `dsy_servicekit.lifecycle`; produces `packages/workflow/tests/service/test_contract.py` and the cached loaders.
- Needs: CP1 merged.

**WFL-2 `control_worker` builder; split the generic connect out of `activities/db.py`** - S, mech
- Scope: `dsy_workflow.worker.control_worker` builds the control worker the service builds at `service/__init__.py:229`; `activities/db.connect` becomes a call to `pg.connect(dsn, autocommit=..., lock_timeout="10s")`, keeping `typed_errors` where it is; the service builds its workflow, control and reaper workers through the builders and `dsy_temporal.connect(..., check_namespace=True)`.
- Criteria: existing tests pass; `activities/db.py` still exports `connect` and `typed_errors`; `grep -rn 'Client.connect(' packages/workflow/src/dsy_workflow/service` prints nothing.
- Verifier: `scripts/itest.sh workflow`.
- Consumes / produces: consumes `dsy_servicekit.pg` and `dsy_temporal.connect`; produces `dsy_workflow.worker.control_worker`.
- Needs: WFL-1, L1.

**WFL-3 The reaper worker drains, and the reaper connects with a timeout (H1, H3)** - S, judge (Q4)
- Scope: `schedule.py:119` gets `graceful_shutdown_timeout` equal to the workflow's `DRAIN`; `schedule.py:97` and `__main__.py:56` use `pg.connect`; the CLI's three plain `Client.connect` (`schedule.py:171,178,188`) go through `dsy_temporal.connect` only after a check that the reaper's payloads are plain dicts, which the scout left unverified; `dsy-servicekit` is added to `packages/artifactreaper/pyproject.toml` under `[project] dependencies` and `[tool.uv.sources]`.
- Criteria: a test shows a pass in flight at shutdown finishes inside `DRAIN` (fails first with the SDK default of 0); the payload check is written in the pull request.
- Verifier: `scripts/itest.sh artifactreaper`.
- Consumes / produces: consumes `dsy_servicekit.pg` and `dsy_temporal.connect`; produces a drained reaper worker.
- Needs: WFL-2.
  Lands before CP2.

### Lane WRK: pool worker

**WRK-1 The worker adopts the helpers and `dsy_temporal.connect`** - S, mech
- Scope: `ConfigError` alias, `require` for `config.py:57`, `ReadyFile` (required there, so `ReadyFile` is constructed with a path), `stopping` for `service.py:55`, `dsy_temporal.connect(..., check_namespace=True)` for `service.py:59-67`; `dsy-servicekit` is added to `packages/worker/pyproject.toml` under `[project] dependencies` and `[tool.uv.sources]`; its service boot test (`tests/service/`, for obs the exporter's test) and the `test_contract.py` it creates are marked `seam`.
  The worker calls `load()` before `setup()` today (`__main__.py:22-23`), so a config error is the one line not in the JSON log; the order is kept.
- Criteria: as ADM-1 for the worker, including the `uv export` line, with SIGTERM exit 0 under 70 s (grace 75 s minus 5 s).
- Verifier: `scripts/test.sh packages/worker`, `scripts/itest.sh worker`, `pytest deploy/workers -q`.
- Consumes / produces: consumes `dsy_servicekit` and `dsy_temporal.connect`; produces `packages/worker/tests/service/test_contract.py`.
- Needs: CP1 merged, L1.

**WRK-2 `DSY_TEMPORAL_NAMESPACE` for the worker (H4)** - S, judge (Q4)
- Scope: `worker/config.py:14`, `deploy/workers/pool.star:89`, `deploy/workers/test_render.py:145`, in one commit.
- Criteria: the render test asserts `DSY_TEMPORAL_NAMESPACE`; a worker started with only the old variable exits 2 naming the missing one; the pod rolls in the CP2 run.
- Verifier: `pytest deploy/workers -q`, `packages/worker/tests/service/`.
- Consumes / produces: consumes WRK-1; produces the single namespace variable.
- Needs: WRK-1.
  Last in its lane, and lands before CP2.

### Lane PLC: placement

**PLC-1 Placement adopts config and lifecycle** - S, mech
- Scope: `BadEnv` aliases the kit's error; `require` and `positive` and `host_port` replace the local parsers (`service.py:55-58,72-82`, the same host:port parse as dispatch's `_address`); `ReadyFile` replaces `:193-199`; the `SystemExit` signal handler (the SIGTERM and SIGINT registrations, two lines) stays, because placement has no drain by design (a documented trade: the workflow retries placement across the gap, `deploy/placement/Tiltfile.module:62`); `dsy-servicekit` is added to `packages/placement/pyproject.toml` under `[project] dependencies` and `[tool.uv.sources]`; its service boot test (`tests/service/`, for obs the exporter's test) and the `test_contract.py` it creates are marked `seam`.
- Criteria: as ADM-1 for placement, including the `uv export` line, with the SIGTERM check expecting exit 0 and no drain.
- Verifier: `scripts/test.sh packages/placement`, `scripts/itest.sh placement`, `pytest deploy/placement -q`.
- Consumes / produces: consumes `dsy_servicekit`; produces `packages/placement/tests/service/test_contract.py`.
- Needs: CP1 merged.

### Lane OBS: state-inputs

**OBS-1 The exporter adopts config, lifecycle and the connect helper** - S, mech
- Scope: the inline DSN check becomes `require`; `ReadyFile` keeps the exporter's present behaviour (touched after the first database pass, not removed on stop, which OBS-2 changes); `ThreadStop`; `exporter.py:55` uses `pg.connect(dsn, connect_timeout=5, statement_timeout=...)` with the options it has today, so its behaviour is unchanged; `dsy-servicekit` is added to `packages/obs/pyproject.toml` under `[project] dependencies` and `[tool.uv.sources]`; its service boot test (`tests/service/`, for obs the exporter's test) and the `test_contract.py` it creates are marked `seam`.
- Criteria: as ADM-1 for the exporter, including the `uv export` line, with `tests/inputs/test_contract.py` checking exit 2 and SIGTERM exit 0 and not the ready file removal.
- Verifier: `scripts/test.sh packages/obs`, `scripts/itest.sh obs`, `pytest deploy/obs -q -k state`.
- Consumes / produces: consumes `dsy_servicekit`; produces `packages/obs/tests/inputs/test_contract.py`.
- Needs: CP1 merged.

**OBS-2 The exporter removes its ready file on stop (H6)** - XS, judge (Q4)
- Scope: the exporter clears its ready file on stop, as the other five services do (closing the one divergence of scout F3).
- Criteria: a test first shows the file left behind after SIGTERM, then passes; the contract test gains the ready file lifecycle check.
- Verifier: `packages/obs/tests/inputs/test_contract.py`.
- Consumes / produces: consumes `dsy_servicekit.lifecycle.ReadyFile.clear`; produces the removal on stop.
- Needs: OBS-1.
  Lands before CP2.

### Lane DB: Postgres logins

**DB-1 `dsy_clusterkit.logins`** - M, mech
- Scope: the nine shared functions of the four `logins.py` (`state_file`, `secrets_of_logins`, `secret`, `MEMBER_OF`, `ATTRIBUTES`, `state`, `converge`, `logs_in`, `main`) move into one module taking the per-service data; `state_file` uses `dsy_clusterkit.state_dir()`; the temp file is written atomically with a process-unique name, as `deploy/rabbitmq/config.py:46-65` does.
- Criteria: `scripts/itest.sh clusterkit` converges each of the four services' logins against the appdb fixture and `check` reports no difference; two processes started together leave one password; a recorded diff shows the SQL `alter role ... nosuperuser nocreaterole nocreatedb noreplication nobypassrls valid until 'infinity'` appears once in the repo (`grep -rn nobypassrls deploy packages/clusterkit` has one source hit).
- Verifier: `packages/clusterkit/tests/test_logins.py`.
- Consumes / produces: consumes `dsy_clusterkit.state_dir()` and `appdb_password()`; produces `dsy_clusterkit.logins.main`.
- Needs: L0.

**DB-2 The four `logins.py` become data files** - S, mech
- Scope: each of `deploy/{admission,dispatchapi,workflow,obs/state-inputs}/logins.py` keeps its path and its CLI (`render`, `apply DSN`, `check DSN`) and shrinks to the data (`LOGINS`, `NAMESPACE`, `STORE_ROLES` where present) plus one call to `dsy_clusterkit.logins.main`.
- Criteria: the four `deploy/*/test_logins.py` pass unmodified; each file is at most 40 lines; `render` output is byte-identical to the old output for each service (diff pasted).
- Verifier: `scripts/test.sh deploy -k logins`.
- Consumes / produces: consumes `dsy_clusterkit.logins.main`; produces the four data files.
- Needs: DB-1.

**DB-3 One `logins.sh`** - S, mech
- Scope: `scripts/storage/db/logins.sh <service> [apply|check]` holds the port-forward wait loop once; each `deploy/*/logins.sh` becomes a wrapper of at most 5 lines with its path unchanged, so no `Tiltfile.module` changes.
- Criteria: `grep -rn 'Forwarding from' deploy/*/logins.sh deploy/obs/state-inputs/logins.sh` prints nothing; the wrappers pass `shellcheck` through `scripts/lint.sh`; on the cluster in CP2, `just up` and the module deploy apply the four logins and `just cluster-verify` passes.
- Verifier: `scripts/lint.sh` and the CP2 cluster rows.
- Consumes / produces: consumes DB-2; produces `scripts/storage/db/logins.sh`.
- Needs: DB-2.

### Lane TOOL: scripts

**TOOL-1 One cluster lock helper** - S, mech
- Scope: `scripts/cluster/lock.sh` with `cluster_lock <exit_code>`: takes fd 9 on `/tmp/dsy-cluster.lock`, waits `DSY_CLUSTER_LOCK_TIMEOUT` (default 900), exits with `<exit_code>` and a message when the wait runs out, and always exports `DSY_CLUSTER_LOCK_HELD=1`; `smoke.sh` passes 4 and the other four blocks pass the exit they use today (1), so no behaviour moves in this issue.
- Criteria: `packages/acceptance/tests/test_wrapper.py` passes with its fake `flock` for all five scripts; a new test runs two lock-taking scripts in sequence and shows no 900 s wait; `grep -rn 'exec 9>' scripts` prints only `lock.sh`.
- Verifier: `scripts/test.sh packages/acceptance`.
- Consumes / produces: consumes `flock` and the fd 9 convention; produces `cluster_lock <exit_code>`.
- Needs: none.
- Gate: decision Q5 (only L0 starts before the e2e gate).

**TOOL-2 Doctor self-test in CI; `DSY_CONTEXT` override removed** - XS, mech
- Scope: `scripts/ci.sh` runs `scripts/doctor.test.sh`; `allocatable.sh:13`, `canary.sh:19`, `namespaces.sh:24` hard-code `context=kind-dsy` like the other eight.
- Criteria: `scripts/ci.sh --checks` runs the doctor self-test (output pasted); `grep -rn DSY_CONTEXT scripts` prints nothing, and `grep -rn DSY_CONTEXT docs --exclude=19-platform-seams.md` prints nothing.
- Verifier: the two commands.
- Consumes / produces: consumes `scripts/doctor.test.sh`; produces one line in `scripts/ci.sh`.
- Needs: none.
- Gate: decision Q5 (only L0 starts before the e2e gate).

**TOOL-3 One state-dir and credential helper** - S, mech
- Scope: `scripts/lib/state.sh` gives `state_dir` and `ensure_credential <name> <bytes>` with an atomic write; the 8 shell copies of the state-dir expression (including `scripts/cluster/obs.sh`, `scripts/storage/store/identities.sh`, and the state lines of `up.sh`, `nuke.sh` and `cluster-verify.sh`) and the two credential scripts (`secret.sh`, `secrets.sh`) plus `deploy/obs/grafana/secrets.sh` call it, keeping every file name the Python side reads (`appdb.credential`).
- Criteria: `grep -rn 'DSY_STATE_DIR:-' scripts deploy` prints only `state.sh`; `scripts/storage/db/secrets.sh password` returns the same value before and after on an existing state directory; `dsy_clusterkit.appdb_password()` still reads it.
- Verifier: `scripts/lint.sh`, `scripts/test.sh packages/clusterkit`.
- Consumes / produces: consumes the state directory layout; produces `scripts/lib/state.sh`.
- Needs: none.
- Gate: decision Q5 (only L0 starts before the e2e gate).

**TOOL-4 One lock exit, and the two missing locks (H7, cleanup call 2)** - S, judge (Q4)
- Scope: every caller of `cluster_lock` passes 4, so `up.sh`, `down.sh`, `nuke.sh` and `cluster-verify.sh` exit 4 on a lock timeout where they exit 1 today (`up.sh:27`); `scripts/tilt/run.sh` locks for `ci` and `soak` only, and `dev` stays unlocked and says so in `docs/runbooks/operator.md`; `canary.sh` locks.
  `state-modules.yml` already runs `tilt/run.sh` where the lock is free.
- Criteria: the wrapper test first shows exit 1 on a lock timeout for `up.sh`, then exit 4 after; it covers `tilt/run.sh ci`, `tilt/run.sh soak`, `tilt/run.sh dev` (no lock) and `canary.sh`; `docs/runbooks/operator.md:15` and `docs/acceptance-ladder.md:63` stay true.
- Verifier: `scripts/test.sh packages/acceptance`.
- Consumes / produces: consumes `cluster_lock <exit_code>`; produces exit 4 on every lock timeout.
- Needs: TOOL-1.
  The policy is decided (cleanup call 2): lock both, exit 4 everywhere.
- Gate: decision Q5 (only L0 starts before the e2e gate).

### Lane TS: test support (library side, additive)

**TS-1 `db` and `login` fixtures in `dsy_migrate.fixtures`** - M, mech for `db`, judge for `login`
- Scope: `db` (superuser autocommit connection) and a `login(group)` factory returning a DSN, with the strongest teardown (terminate backends, `DROP OWNED BY`, `DROP ROLE`); no call site changes.
- Criteria: `scripts/itest.sh migrate` passes; a test shows a leaked connection does not break teardown; the list of the 11 files that create roles, each with its replacement, is in the pull request.
- Verifier: `packages/migrate/tests/`.
- Consumes / produces: consumes the appdb fixture; produces `db` and `login`.
- Needs: L0.

**TS-2 `shelf`, OTLP receiver, broker fixture, canonical `free_port`, shared deadline logic for `until`** - M, mech
- Scope: `shelf` in `dsy_storecheck.fixtures`; a gzip-aware OTLP receiver in `dsy_obs.testing` (the dispatch one is the base); a `broker` fixture factory in `dsy_broker.testing`; `dsy_clusterkit` re-exports `free_port` from `dsy_servicekit.testing`, one implementation, and keeps its async `until` (`every_s`) as a thin async wrapper over the same deadline logic or as its own, signature unchanged, because `dsy_servicekit.testing.until` is sync; `dsy-servicekit` is added to `packages/clusterkit/pyproject.toml` under `[project] dependencies` and `[tool.uv.sources]`.
- Criteria: each has a test; `dsy_clusterkit.free_port` and `until` keep their signatures (`until` still accepts awaitables and `every_s`).
- Verifier: `scripts/test.sh packages/obs packages/clusterkit`, `scripts/itest.sh storecheck`.
- Consumes / produces: consumes `dsy_servicekit.testing`; produces `shelf`, the OTLP receiver and the `broker` fixture.
- Needs: L0.

**TS-3 `dsy_workflow.testing`: dev server, client, rig, builders** - L, judge
- Scope: factories for the Temporal dev server with the parameters the 12 sites use (scope, namespace, `--db-filename`, search attributes from `naming.yaml` as a layer); `client()` with the tracing interceptor; a `Rig` skeleton (workflow worker through `workflow_worker`, a control worker, `versioning.set_current`, ordered shutdown) with plan, emit, pool script and the placer as parameters, the placer supplied by the caller and never built inside `dsy_workflow/testing`, so the one `workflow_worker(.*StaticPlacer()` line of the harness stays the only one in `packages/*/src`; `accepted_event(...)` and a plan builder from `workflow/tests/workflow/conftest.py:64 _plan`; one `build_id` fixture replacing the module-level `os.environ.setdefault` at 4 conftests; `make_current` replaced by `versioning.set_current`, whose `TimeoutError` is stricter (scout, C F5).
- Criteria: each factory has a test in `packages/workflow/tests/`; `dsy_workflow.testing.connect(address, attempts=...)` keeps its signature (`acceptance/clients/temporal.py:11`); the fixtures are factories with a one-line wrapper per consumer, which is the default, and become an entry-point plugin only if spike S1 passes.
- Verifier: `scripts/test.sh packages/workflow`, `scripts/itest.sh workflow`, `scripts/itest.sh acceptance`.
- Consumes / produces: consumes `workflow_worker` and `control_worker`; produces the `dsy_workflow.testing` factories and a `Rig` that takes the placer as a parameter.
- Needs: L0.
  Spike S1 only decides plugin versus wrapper, and the wrapper is the default.

### Lanes RIG: consumers migrate, each pull request deleting what it replaces

**RIG-A-1 Admission tests** - M, mech
- Scope: the three admission conftests (`core`, `scan`, `service`) use the TS fixtures; the identical `row_of`, `status`, `server`, `client`, `GatedEmitter`, `SHA`, `MANIFEST` move to one `tests/support.py`; `tests/scan/test_resume.py:29`, which has no deadline of its own, gets one.
- Criteria: `grep -rn 'ThreadPoolExecutor(' packages/admission/tests` prints nothing; `scripts/itest.sh admission` passes with the same test count (counts pasted before and after).
- Verifier: that command.
- Consumes / produces: consumes the TS fixtures; produces `packages/admission/tests/support.py`.
- Needs: TS, ADM.

**RIG-D-1 Dispatch tests** - M, mech
- Scope: the dispatch conftests use the shared dev server, `db`, `login`, `shelf` and the `Rig`; `workflow_dsn` gets the strong teardown; the dispatch OTLP receiver is replaced by `dsy_obs.testing`.
- Criteria: `grep -rn 'workflows=\[JobWorkflow\]' packages/dispatchapi/tests` prints nothing; `scripts/itest.sh dispatchapi` passes with the same test count; `scripts/test.sh packages/dispatchapi` passes.
- Verifier: `scripts/itest.sh dispatchapi`, `scripts/test.sh packages/dispatchapi`.
- Consumes / produces: consumes the TS fixtures and `dsy_obs.testing`; produces nothing new.
- Needs: TS, DIS.

**RIG-W-1 Workflow, temporal and worker tests: dev server, `make_current`, build id, and the worker's own copies** - M, judge
- Scope: the 4 conftests that start `temporalio/temporal`, `make_current`, the four build-id lines, and `workflow/tests/test_worker.py`, `replay/record.py`, `versioning/test_versioning.py`'s `control_worker`, which builds the queue by string replace instead of calling `control_queue`; the `db`, `login` and `free_port` copies in `packages/worker/tests`; and the workflow package's own copies: the `db` fixture and `CREATE ROLE` in `packages/workflow/tests/activities/conftest.py`, the `free_port` binds in `tests/service/conftest.py`, `tests/service/test_job_counters.py` and `tests/versioning/test_versioning.py`.
- Criteria: `grep -rn --include='*.py' -E 'def db\(|CREATE ROLE|bind\(\("127.0.0.1", 0\)\)' packages/workflow/tests packages/worker/tests` prints nothing; `grep -rln '"temporalio/temporal"' packages deploy` lists at most 5 files, each named in the pull request with the reason it differs (function scope, `--db-filename`, no namespace); `scripts/itest.sh workflow` and `scripts/itest.sh worker` pass.
- Verifier: `scripts/itest.sh workflow`, `scripts/itest.sh worker`.
- Consumes / produces: consumes the TS fixtures; produces nothing new.
- Needs: TS, WFL, WRK.

**RIG-W-2 The three rigs that bypass `workflow_worker`, and the seven control workers** - S, mech
- Scope: closes #464's literal ask: `admission/tests/scan/conftest.py:326`, `admission/tests/service/conftest.py:288`, `dispatchapi/tests/control/conftest.py:196` and the seven `Worker(client, task_queue=control_queue(...)` copies, plus the `Rig`'s control worker, use `workflow_worker` and `control_worker`.
- Criteria: `grep -rn 'place_activity(StaticPlacer())' packages/*/tests` lists only the two kept workflow files, `packages/workflow/tests/test_worker.py` and `packages/workflow/tests/test_placer_contract.py`, and `grep -rn 'workflow_worker(.*StaticPlacer()' packages/*/src` lists exactly one line, `packages/workflow/src/dsy_workflow/harness/__init__.py:438` (the harness helper, one hit before and after); `packages/workflow/tests/test_worker.py:138` still shows the placer on a `dsy-workflow-activity` thread, and a copy of that assertion runs through an admission rig.
- Verifier: `scripts/itest.sh admission`, `scripts/itest.sh dispatchapi`, `scripts/itest.sh workflow`.
- Consumes / produces: consumes `workflow_worker`, `control_worker` and the `Rig`; produces nothing new.
- Needs: TS-3, RIG-A-1, RIG-D-1 (it edits the same rigs after they migrate).

**RIG-R-1 The remaining `db`, `login`, `shelf` and `free_port` copies** - M, mech per file
- Scope: the tests of `artifactclient` (including `test_commit.py` and `test_cache.py`, which create roles), `artifactreaper`, `stateops`, `jobstate`, `eventstore`, `artifactindex`, `placement`, `obs`, `storecheck`, `compile` and `acceptance`.
  The admission, dispatch and worker copies belong to RIG-A-1, RIG-D-1 and RIG-W-1, and the workflow copies to RIG-W-1.
- Criteria: the three greps run over its own paths only, `RIGR="packages/artifactclient/tests packages/artifactreaper/tests packages/stateops/tests packages/jobstate/tests packages/eventstore/tests packages/artifactindex/tests packages/placement/tests packages/obs/tests packages/storecheck/tests packages/compile/tests packages/acceptance/tests"` (a plain list for bash, used unquoted, and `ls -d $RIGR >/dev/null` runs first so that a missing path fails loudly): `grep -rln -E 'bind\(\("127.0.0.1", 0\)\)' $RIGR` prints nothing; `grep -rn --include='*.py' -E 'def db\(' $RIGR` lists at most the `eventstore` wrapper; `grep -rln 'CREATE ROLE' $RIGR` lists at most 2 files, each named.
  The `until` copies are replaced only where the timeout semantics match.
- Verifier: `scripts/test.sh` and `scripts/itest.sh` for each package touched, and the three greps.
- Consumes / produces: consumes the TS fixtures; produces nothing new.
- Needs: TS, PLC, OBS.

**RIG-R-2 The OTLP receivers, the root fixture clash, the conftest path loaders** - S, judge
- Scope: the OTLP receivers outside dispatch (which RIG-D-1 migrates) use `dsy_obs.testing`; the `obs/tests/logs/conftest.py:7` `root` fixture is renamed; the four sibling-conftest loaders of scout F16 are removed where an importable home now exists.
- Criteria: `scripts/test.sh packages/obs packages/placement` passes.
- Verifier: that command.
- Consumes / produces: consumes `dsy_obs.testing`; produces nothing new.
- Needs: TS, RIG-R-1.

**RIG-R-3 One marker registration with `--strict-markers`** - XS, mech
- Scope: `pair`, `isolated` and `cluster` registered with `integration`, `slow` and the `seam` marker of L0-3 in the root `pyproject.toml`; `--strict-markers` on.
- Criteria: `scripts/test.sh` and `scripts/itest.sh` collect the same number of tests as before (counts pasted) and no `PytestUnknownMarkWarning` remains.
- Verifier: the two collect counts.
- Consumes / produces: consumes the `seam` marker of L0-3; produces one marker block.
- Needs: RIG-R-1.

### Lane MOD: the module seam

**MOD-1 `dsy_workflow.modules` and the loop in `serve`** - S, judge
- Scope: section 5.2; the three built-in workers and the supervision logic of `_run_until` unchanged (its ready-file lines moved to `ReadyFile` in WFL-1).
- Criteria: `packages/workflow/tests/test_modules.py` has a duplicate-name unit test over faked entry points, a test that a module raising on import or in construction is skipped and logged at error level while `JobWorkflow` still runs, a test that a module worker dying later is logged at error level by its supervisor and does not end the host while the built-in workers still serve, a test with a module function defined in the test and fed through a faked `load` that an activity in flight at SIGTERM finishes inside `DRAIN` because the supervised module worker is shut down with the others, and a no-module path test that hides the entry-point group and runs the existing `serve` service tests; `scripts/test.sh packages/workflow` and `scripts/itest.sh workflow` pass.
- Verifier: that file.
- Consumes / produces: consumes WFL-2's builders; produces `dsy_workflow.modules` (`Context`, `load`) and the loop in `serve`.
- Needs: WFL, TS-3.

**MOD-2 `packages/exampleflow`** - S, mech
- Scope: section 5.3; the example module's tests are marked `seam`.
- Criteria: `scripts/itest.sh exampleflow` serves the host with `exampleflow` installed and completes an `EchoWorkflow` and a level 0 job in the same process; the wheel test installs the example's wheel closure into a clean virtualenv and finds the entry point; `git diff --name-only origin/main...HEAD` prints only paths under `packages/exampleflow/`, the root `pyproject.toml` (the workspace source and the dev group, two lines) and `uv.lock`.
- Verifier: the commands above, the diff output pasted.
- Consumes / produces: consumes `dsy_workflow.modules`; produces `packages/exampleflow`.
- Needs: MOD-1.

**MOD-3 Seam smoke wiring and the measured budget** - S, mech
- Scope: no test file is touched, because every `seam` mark is owned by the issue that creates or adopts the test; `scripts/seam.sh` timer set from one measured run; the ladder row's budget replaced by the measurement.
- Criteria: `nix develop -c just seam` exits 0 and prints one summary line per group (boot, contract, example module, layering) with the total seconds, as built in L0-3; the same tests still run inside `just itest <domain>`.
- Verifier: `nix develop -c just seam`, output pasted.
- Consumes / produces: consumes `just seam` and the service boot tests; produces the measured budget.
- Needs: MOD-2, every service lane merged.

### Lane EXIT

**EXIT-1 The epic exit test and Closure** - S, mech
- Scope: section 10, run by a worker who did not write the lanes; Closure filled; architecture note refined.
- Criteria: every row of section 10 has its evidence file, and the Closure section names the commit, the CI run and the ledger result.
- Verifier: the evidence files `exit-test/<row>.txt` pasted in the pull request.
- Consumes / produces: consumes every issue above; produces the Closure.
- Needs: every issue above, including RIG-A-1, RIG-D-1, RIG-W-1, RIG-W-2 and RIG-R-1 to 3, because E-10 to E-12 are tree-wide.

## 8. Order, stubs and spikes

### 8.1 Order

```
Phase 1:  L0 (library and its own tests; the only lane that may start before the gate)
              |
Gate:     issue #682 delivered, `just e2e` passes on main
              |
Phase 2a: L1, then ADM (pathfinder) ... CP1;  DB, TOOL, TS run beside it (disjoint paths)
                  |
Phase 2b: DIS  WFL  WRK  PLC  OBS, then ADM-3 and ADM-4 (parallel, at most four at once) ... CP2
                  |
Phase 3:  RIG-A (after ADM, TS)   RIG-D (after DIS, TS)   RIG-W (after WFL, WRK, TS)   RIG-R (after PLC, OBS, TS)
                  |
Phase 4:  MOD (after WFL, L0, TS)    EXIT
```

- Lane 0 is L0, the only barrier for the library.
  Nothing else starts before the gate, because the e2e is part of the net that the refactor of six services rests on (decision Q5).
- L0 may start while #17 pass 2 is open because it touches no product file except the ladder row, which is a shared file with a rule (section 6).
  This departs from `docs/delivery/method.md:107,165,238-239`, which allow only runtime spikes beside the one active epic, open an epic only when everything it consumes is closed, and forbid overlap on product files, and the captain accepts the departure by approving this spec.
- L1 and TOOL start at the gate without waiting for L0 to merge, which departs from `docs/delivery/method.md:7`: their paths are disjoint from L0 (`packages/temporal/src/dsy_temporal/client.py`, shell scripts) and nothing consumes them until ADM-2 and the lock callers, so a merge order between them and L0 changes no file.
  DB and TS consume L0 and wait for it.
- Behaviour issues (H1 to H7) are the last issues of their lanes, so the mechanical adoption of every service has merged before behaviour moves.
- CP1 is after ADM-1 and ADM-2, the mechanical pathfinder only: three consecutive `just smoke` runs on one deploy, then `just e2e`, then `python -m dsy_dispatchapi.smoke --scenario pause-resume`, `cancel-before-start` and `restart`.
  CP2 is the same after every behaviour issue (ADM-3, ADM-4, DIS-3, DIS-4, WFL-3, WRK-2, OBS-2, TOOL-4) and every adoption issue (DIS-1, DIS-2, WFL-1, WFL-2, WRK-1, PLC-1, OBS-1) and DB-3 have merged, with ADM-4 and DIS-4 inside CP2.
  The CP2 worker also runs the live scrapes of ADM-3 and DIS-3.
  One worker runs each checkpoint, and it is not the author of the lanes it checks.
  Each checkpoint takes the cluster lock once, either by letting each script lock itself or by running its commands inside one `flock /tmp/dsy-cluster.lock` with `DSY_CLUSTER_LOCK_HELD=1` exported, never a nested lock.
- No stubs: nothing is faked ahead of its owner.
  Every new piece is real and unused until adopted, and the toy service in `packages/servicekit/tests/toy/` is the reference consumer that proves the kit before any real service adopts it.
- Contract test of each stub: not applicable, there are none.

### 8.2 Spikes

| Id | Question | Pass line | Blocks |
| --- | --- | --- | --- |
| S1 | Can `dsy_workflow.testing` fixtures be shared through an entry-point plugin or `pytest_plugins` without breaking `--import-mode=importlib` (`pyproject.toml:90`) and without an import cost on sessions that never use them? | with the plugin installed, `scripts/test.sh packages/obs` collects the same tests and its wall time grows by less than 0.5 s over three runs, and a run of the whole tree collects without a `pytest_plugins` error | the plugin form of TS-3 only. TS-3 starts with factories and a one-line wrapper per consumer, which is the default and needs no spike |
| S2 | Does the blackhole socket test reproduce the unbounded Postgres connect on the unchanged code, and what does the `to_thread` caller in admission do while it hangs? | the test fails against `psycopg.connect(dsn, autocommit=True)` with a TimeoutError of the test's own, and the admission cycle's behaviour is written down | ADM-4 and DIS-4. It is the "reproduce first" rule of the repo's engineering guide, run as the first commit of the issue and not a separate pull request |

Both spikes write only a note under `docs/delivery/spikes/` and throwaway code beside it, as the method asks.

## 9. Test delta

- New packages: `packages/servicekit` (unit tests, a blackhole test, integration tests marked `integration` and `pair`, the layering test, a toy service) and `packages/exampleflow` (its own tests).
- New tests in existing packages: one `tests/service/test_contract.py` per service (six, a few lines each over the kit), `test_client.py` in `temporal`, `test_logins.py` in `clusterkit`, `test_modules.py` in `workflow`, the serve test in `exampleflow`, bucket tests in admission and dispatch, the cache test in workflow, extended `test_wrapper.py` in `acceptance`.
- Removed tests: none, with one exception: duplicated helper definitions in conftests go, and a test that only asserted a helper's own copy goes with it, each named in its pull request.
- New helper scripts: `scripts/cluster/lock.sh`, `scripts/lib/state.sh`, `scripts/seam.sh`, `scripts/storage/db/logins.sh`.
- New recipe: `just seam`.
- New marker: `seam`.
- Test counts: every RIG issue records the collected test count before and after, and a drop needs a named reason.
- Stays untested, and why: the real kubelet SIGTERM drain, a Postgres failover and the live `/metrics` scrape (section 4.3); the example module on a cluster (Q3); kind C, a new task type (Q1).
- Known flaky test: `packages/dispatchapi/tests/control/test_retrier.py::test_request_during_outage_delivered_after` (scout, from `docs/delivery/epics/17-acceptance-smoke.md:295`, not re-run here) lives in the file DIS-4 and RIG-D-1 edit.
  DIS-4 runs it 20 times before and after, and per the repo's standard a fix for it, if the cause shows, is in scope.

## 10. Exit test

Run by a worker who did not write the lanes, on a clean `git archive` export of `main` taken outside any git repository, with Docker running and the cluster free for rows C1 to C7.
Each command runs from the export root as `nix develop -c`, and Python commands take `uv run --frozen`.
Inside a table cell `\|` is the Markdown escape for `|`, so it means a plain `|` (alternation or pipe) when the command is run, and the exit worker runs the unescaped command, never the raw cell text.
Each row's real output is the evidence file `exit-test/<row>.txt` beside the export, pasted in the pull request that records the run and summarised in Closure.
The export has no git checkout, so `DSY_BUILD_ID` is set for cluster rows (the workflow module requires it outside one), and counts use `grep -rn --include='*.py'`.

```
EXPORT=$(mktemp -d)            # outside any git repository
git -C <repo> archive origin/main | tar -x -C "$EXPORT"
( cd "$EXPORT" && <command> )
```

### 10.1 Per-lane exit tests

| Row | Lane | Command | Expected |
| --- | --- | --- | --- |
| X-L0 | L0 | `uv sync --frozen && uv lock --check`; `pytest packages/servicekit -q -rs`; `pytest packages/servicekit/tests/test_layering.py -q`; `scripts/check-contracts-map.sh`; `scripts/check-links.sh`; `scripts/export-diagrams.sh --check`; `just seam` | each exit 0, no `SKIPPED` line, `just seam` runs the toy's seam tests |
| X-L1 | L1 | `pytest packages/temporal -q -k client`; `scripts/itest.sh temporal` | pass; the integration run shows converter, interceptor and SDK request metrics on the passed runtime |
| X-ADM | ADM | `pytest packages/admission -q -rs`; `scripts/itest.sh admission`; `pytest deploy/admission -q`; `pytest packages/admission/tests/service/test_contract.py -v`; `grep -rn --include='*.py' -E "if missing :=\|add_signal_handler\|PrometheusConfig\|Client.connect\(" packages/admission/src` | tests pass with no skip; the grep prints nothing |
| X-DIS | DIS | the same four test commands for `dispatchapi`; `grep -rn --include='*.py' -E "if missing :=\|signal.signal\|PrometheusConfig" packages/dispatchapi/src`; `grep -n 'Client.connect(' packages/dispatchapi/src/dsy_dispatchapi/gateway.py`; `grep -rnw 9090 deploy/dispatchapi docs/runbooks \| grep -v '19090:9090'` | tests pass; the greps print nothing (`Client.connect(` is searched in `gateway.py` only, as E-3 does, because the smoke driver `smoke/__init__.py:446,481` keeps its own client by design, section 13; the last only if H5 shipped; the closed epics 13 and 18 are history and are not searched) |
| X-WFL | WFL | the same test commands for `workflow` and `artifactreaper`; `grep -rn --include='*.py' -E "if missing :=\|add_signal_handler\|Client.connect\(" packages/workflow/src/dsy_workflow/service` | pass; nothing printed; `naming()` read once per process (the cache test) |
| X-WRK | WRK | the same for `worker`; `grep -rn 'DSY_NAMESPACE\b' packages deploy` | pass; the grep prints nothing after H4 (`DSY_TEMPORAL_NAMESPACE` does not match it) |
| X-PLC | PLC | the same for `placement` | pass |
| X-OBS | OBS | the same for `obs` and `pytest deploy/obs -q -k state` | pass; the ready file is gone after a stop (H6) |
| X-DB | DB | `pytest deploy -q -k logins`; `scripts/itest.sh clusterkit`; `wc -l deploy/*/logins.py deploy/obs/state-inputs/logins.py`; `grep -rn nobypassrls deploy packages/clusterkit` | pass; each file at most 40 lines; one source hit |
| X-TOOL | TOOL | `pytest packages/acceptance/tests/test_wrapper.py -q`; `scripts/doctor.test.sh`; `grep -rn 'exec 9>' scripts`; `grep -rn DSY_CONTEXT scripts`; `grep -rn DSY_CONTEXT docs --exclude=19-platform-seams.md` | pass; `lock.sh` is the one hit of the third; the fourth and fifth print nothing |
| X-TS | TS | `pytest packages/migrate packages/storecheck packages/obs packages/workflow packages/clusterkit -q`; `scripts/itest.sh migrate`; `scripts/itest.sh storecheck` | pass |
| X-RIG | RIG | the copy ledger rows of section 10.2 for tests; `scripts/itest.sh admission`; `scripts/itest.sh dispatchapi`; `scripts/itest.sh workflow` | pass with the recorded test counts |
| X-MOD | MOD | `pytest packages/workflow/tests/test_modules.py -v`; `scripts/itest.sh exampleflow`; `pytest packages/exampleflow -q -k wheel`; `just seam` | pass; `just seam` prints one summary line per group and the total seconds |

### 10.2 The copy ledger

Rows E-10 to E-12 stay tree-wide and are exit-test checks: EXIT-1 runs them only after all of phase 3 (RIG-A, RIG-D, RIG-W and RIG-R) has merged, and each RIG issue greps only its own paths.
The numbers in "Before" were measured with the same commands on `main` at `ee88fc7` (a worktree, not an export, which makes no difference to `grep -rn`).
"After" is the target, and a row that misses it needs a named reason in Closure.

| Row | Pattern | Command (from the root) | Before | After |
| --- | --- | --- | --- | --- |
| E-1 | the missing-environment check | `grep -rn --include='*.py' 'if missing :=' packages/*/src` | 4 | 0 |
| E-2 | config error classes | `grep -rn --include='*.py' -E '^class (ConfigError\|MissingEnv\|BadEnv)\b' packages/*/src` | 5 | 1 (the kit's) |
| E-3 | service Temporal clients built by hand | `grep -rn --include='*.py' 'Client.connect(' packages/admission/src packages/dispatchapi/src/dsy_dispatchapi/gateway.py packages/workflow/src/dsy_workflow/service packages/worker/src` | 4 | 0 |
| E-4 | hand-built metrics runtimes | `grep -rn --include='*.py' 'PrometheusConfig(' packages/*/src` outside `obs/src` | 2 | 0 |
| E-5 | signal wiring in service source | `grep -rn --include='*.py' -E 'add_signal_handler\|signal\.signal\(' packages/*/src \| grep -v -E 'packages/(itest\|servicekit)/'` | 7 lines (admission, dispatch, obs, worker and workflow once each, placement twice) | exactly 2, both in `packages/placement/src/dsy_placement/service.py` (the SIGTERM and SIGINT registrations), kept by design and named |
| E-6 | unbounded service connects | `grep -rn --include='*.py' 'psycopg.connect(' packages/admission/src packages/dispatchapi/src/dsy_dispatchapi/retrier.py packages/dispatchapi/src/dsy_dispatchapi/reconciler.py packages/artifactreaper/src` | 7 | 0 |
| E-7 | cluster lock blocks | `grep -rn 'exec 9>' scripts` | 5 | 1 |
| E-8 | login module lines | `cat deploy/admission/logins.py deploy/dispatchapi/logins.py deploy/workflow/logins.py deploy/obs/state-inputs/logins.py \| wc -l` | 668 | at most 160 |
| E-9 | hand-started Temporal dev servers | `grep -rln --include='*.py' '"temporalio/temporal"' packages deploy` | 12 | at most 5, each named |
| E-10 | `db` fixtures | `grep -rn --include='*.py' 'def db(' packages/*/tests` | 9 | at most 1 |
| E-11 | test files that create roles | `grep -rln --include='*.py' 'CREATE ROLE' packages/*/tests` | 11 | at most 2, each named |
| E-12 | own free-port sockets in tests | `grep -rln --include='*.py' -E 'bind\(\("127.0.0.1", 0\)\)' packages/*/tests deploy` | 16 | 0 |
| E-13 | executors made by admission and dispatch test rigs | `grep -rn --include=conftest.py 'ThreadPoolExecutor(' packages/admission/tests packages/dispatchapi/tests` (the two `with ThreadPoolExecutor(1) as pool:` blocks in `packages/dispatchapi/tests/control/test_routes.py:221,242` are not rig executors and are not counted) | 3 | 0 |
| E-14 | uncached contract readers | `grep -n -B1 -E '^def (naming\|pools)' packages/workflow/src/dsy_workflow/contract.py` | no `@cache` | `@cache` on both |

### 10.3 Epic rows

| Row | Command | Expected |
| --- | --- | --- |
| E-LEDGER | the 14 rows above | each at its target |
| E-CONTRACTS | `( find contracts -type f \| sort \| xargs sha256sum \| sha256sum )` compared with the value recorded at the start of lane L0 | equal. At `ee88fc7` the value is `88e3e6541b03e88f...` over 1,219 files (`git ls-files contracts`); record a fresh one when L0 starts, because other epics may land contract-change pull requests first |
| E-CI | `scripts/ci.sh --checks`, then `scripts/ci.sh --shard workflow`, `--shard admission`, `--shard dispatchapi`, `--shard rest` | exit 0 for each, which is what the CI `check` job runs; one green `check` run on `main`, the URL in Closure. A single `just ci` is about 62 minutes serial (scout section 1.4), so the shards are the practical form |
| E-SEAM | `just seam` | exit 0 inside the measured budget |
| C1 | `just up`, `DSY_BUILD_ID=exit-a scripts/tilt/run.sh ci`, `just cluster-verify`, each taking the cluster lock itself with no outer `flock`, because a script would otherwise wait on a lock its own caller holds (or all three inside one `flock /tmp/dsy-cluster.lock` with `DSY_CLUSTER_LOCK_HELD=1` exported) | exit 0 for each; the four logins applied by `just up` |
| C2 | `just smoke`, three times in a row on the one deploy | exit 0 each, the three summary lines recorded |
| C3 | `python -m dsy_dispatchapi.smoke --scenario pause-resume`, then `cancel-before-start`, then `restart` (the last needs the cluster free of other jobs) | each exits 0 |
| C4 | `kubectl --context kind-dsy -n dsy-control get pods`, then `kubectl --context kind-dsy -n dsy-control logs deploy/admission \| head`, then the same for `deploy/dispatch`, `deploy/workflow-worker` and `deploy/placement` | all Ready; no Python traceback in the start-up lines |
| C5 | on the same deploy, `kubectl ... delete pod` of admission with its grace period, then `just smoke` | exit 0, which shows the shared stop code drains in a real pod |
| C6 | `just doctor`, `just down`, `just nuke` twice | exit 0 for each, and `just nuke` repeats cleanly |
| C7 | `just e2e`, which issue #682 delivers before this epic starts its service lanes | exit 0, and the same result as at the gate |

Not provable by this epic: the cluster half of the example module (Q3), and the T1 queueing behaviour of `just seam` (cleanup call 3).

## 11. Parallelism and cost

Estimates are marked as estimates, because there is no timing data for any of these lanes.

| Phase | Lanes | Workers at once | Issues | Mechanical share | Notes |
| --- | --- | --- | --- | --- | --- |
| 1 | L0 | 1 | L0-1 to L0-3 | 1 of 3 | may start before the e2e gate; L0-1 and L0-2 carry the one judgement, the public surface, which the captain's answer to O2 fixes in advance |
| 2a | L1, ADM, DB, TOOL, TS | 4 | L1-1, ADM-1 to 4, DB-1 to 3, TOOL-1 to 4, TS-1 to 3 | 10 of 15 | starts after the e2e gate; ADM is the pathfinder, ADM-1 and ADM-2 end at CP1 and ADM-3 and ADM-4 follow it |
| 2b | DIS, WFL, WRK, PLC, OBS | 4 of 5 at once | DIS-1 to 4, WFL-1 to 3, WRK-1 and 2, PLC-1, OBS-1 and 2 | 7 of 12 | the behaviour issues DIS-3, DIS-4, WFL-3, WRK-2 and OBS-2 are here; ends at CP2 |
| 3 | RIG-A, RIG-D, RIG-W, RIG-R | 4 | RIG-A-1, RIG-D-1, RIG-W-1 and 2, RIG-R-1 to 3 | 5 of 7 | RIG-W-2 follows RIG-A-1 and RIG-D-1 |
| 4 | MOD, EXIT | 2 | MOD-1 to 3, EXIT-1 | 3 of 4 | MOD-1 touches the file WFL owned |

Totals: 41 issues, 26 mechanical with no per-site judgement and 15 carrying judgement, 8 issues carrying a Q4 behaviour change (ADM-3, ADM-4, DIS-3, DIS-4, WFL-3, WRK-2, OBS-2, TOOL-4) for the seven items H1 to H7, over 17 lanes.
By size: 3 XS, 28 S, 9 M, 1 L (TS-3).
Estimate: phase 1 can start now and then waits for the e2e gate, whose date is not in this epic's hands.
After the gate, a phase is about one to three working days for the lanes in it, and the epic is about eight to twelve working days of wall time at four workers, mostly waiting on the T1 lock and the two checkpoints.

Host constraints:

- The cluster is single-owner.
  Only the two checkpoints (with the live scrapes of ADM-3 and DIS-3) and the exit rows C1 to C7 use it, each under `/tmp/dsy-cluster.lock`, and one worker holds it at a time.
- The T1 lock is host-wide: any run that collects a `pair` test takes both `dsy-t1.*.lock` files and queues every other T1 session on the host, other worktrees and other agents included, with a 1,800 s timeout that fails as `LockTimeout` and reads as a test failure (cleanup call 3).
  Workers should run unit tests first and the integration run last, and the cap of four workers is set by this and not by the amount of work.
- The CI `check` job of each pull request runs on a GitHub runner and does not queue on the T1 lock, so a worker can lean on CI for the full gate and keep the local integration run to its own domain.

What needs the captain: the two open questions O1 and O2, and the closing of #464 (call 12) when the epic is filed.
What is mechanical: every issue marked mech, with the existing tests as the safety net and the copy ledger as the measure.
What is not mechanical: L0-1 and L0-2 (the surface), TS-1's `login` (teardown per site), TS-3 (the dev server parameters and the stricter `set_current`), RIG-W-1, RIG-R-2, the eight Q4 behaviour issues, and MOD-1.

## 12. Closure (filled at close)

Exit test run on a clean `git archive` export of `origin/main` at `9cbb6c6a` (taken outside any git repository, `DSY_BUILD_ID=exit-a`), by a worker who did not write the lanes.
The CI `check` job is green on that commit: https://github.com/AndoNorth/disyard/actions/runs/45995942251.
Evidence files `exit-test/<row>.txt` are pasted in the pull request that records this run.

Per-lane rows (10.1): X-L0, X-L1, X-ADM, X-DIS, X-WFL, X-WRK, X-PLC, X-OBS, X-DB, X-TOOL, X-TS, X-RIG and X-MOD pass.
`just seam` exits 0: 71 s of the 120 s budget.
`scripts/ci.sh --checks` exits 0.
The four `scripts/ci.sh --shard` runs were not repeated: the same suites ran as the per-lane rows above, and the CI `check` job ran them green.
X-OBS skips 6 tests that read a running stack (`-m cluster`).
`deploy/artifactreaper` does not exist, so the "same test commands" for that package ran as `packages/artifactreaper` only.

Copy ledger (10.2), measured on the export:

| Row | After | Target | Result |
| --- | --- | --- | --- |
| E-1 | 1 | 0 | met by design: the kit is the one copy, `packages/servicekit/src/dsy_servicekit/config.py:17` |
| E-2 | 1 | 1 | met (the kit's `ConfigError`) |
| E-3 | 0 | 0 | met |
| E-4 | 0 | 0 | met |
| E-5 | 2 | 2 | met (both in `packages/placement/src/dsy_placement/service.py`) |
| E-6 | 0 | 0 | met |
| E-7 | 1 | 1 | met (`scripts/cluster/lock.sh`) |
| E-8 | 116 | at most 160 | met |
| E-9 | 8 | at most 5 | accepted: `workflow/testing/server.py` (the shared fixture) and `temporal/tests/test_client.py` (tests the client recipe) stay for now; the other 6, baseline dev-server files no lane owned, are tracked in issue 818 |
| E-10 | 1 | at most 1 | met (`packages/eventstore/tests/conftest.py`) |
| E-11 | 1 | at most 2 | met (`packages/migrate/tests/test_fixtures.py`) |
| E-12 | 5 | 0 | accepted: the three blackhole tests (`servicekit/tests/test_pg.py`, `admission` and `dispatchapi` `test_database_outage.py`) need a socket that stays listening, which `free_port` cannot give; the `workflow/tests/test_modules.py` and `temporal/tests/test_client.py` free-port copies are tracked in issue 818 |
| E-13 | 0 | 0 | met |
| E-14 | `@cache` on both | `@cache` on both | met |

E-1 is the kit's implementation of the check and has no other copy.
E-9 and E-12 miss their targets and the captain accepted both, with the reasons in the table; the rest is tracked in issue 818.
The E-9 and E-12 targets in 10.2 were set wrongly: the E-9 arithmetic gave 8 even with perfect lanes, and the E-12 target ignored the blackhole tests the spec prescribes.
E-CONTRACTS: fresh hash `8f5b20f80316cdcc...` over 1,221 files; no start-of-L0 value was recorded, so equality with it is not proven here.

Cluster rows C1 to C7 were met by the CP2 session at `9cbb6c6a`, all PASS with no step retried: `just up`, `scripts/tilt/run.sh ci`, `just cluster-verify`, three `just smoke` runs, the `pause-resume`, `cancel-before-start` and `restart` scenarios, `just e2e`, an admission pod delete followed by `just smoke`, `just doctor`, `just down` and `just nuke` twice.
`just seam` ran 74 s of 120 s there.
CP2 filed issue 816 (the state-inputs exporter crashes on a negative gauge value).

Closure addenda, one dated line each, added after close when a `revisit` child issue is filed against this epic:

- <date>: <link to the revisit issue>, <one-phrase reason>

## 13. Verification of the scout's claims, and what was not checked

Run on `main` at `ee88fc7` from the worktree, with `git grep` and `grep -rn`.

| Claim | Result |
| --- | --- |
| `naming()` and `pools()` are not cached | confirmed, `packages/workflow/src/dsy_workflow/contract.py:106-111` |
| admission and dispatch build their own `PrometheusConfig` | confirmed, `admission/service/__init__.py:115`, `dispatchapi/service/__init__.py:188-190`; `dsy_obs.metrics.runtime` is the construction in the other four (`obs/src/dsy_obs/metrics.py:18`) |
| 4 service Temporal clients, dispatch without a runtime | confirmed, `Client.connect(` at `admission:294`, `workflow:208`, `worker/service.py:59`, `gateway.py:50`, plus the reaper CLI's 3 and 2 in the dispatch smoke driver |
| 5 lock blocks | confirmed, `grep -rn 'exec 9>' scripts` returns exactly the 5 |
| 39 `STUBBED =` lines | confirmed, 39 under `packages/` |
| `DSY_NAMESPACE` only in the worker | confirmed, 3 hits (`worker/config.py:14`, `deploy/workers/pool.star:89`, `deploy/workers/test_render.py:145`) |
| 12 hand-started Temporal dev servers | confirmed, 12 files |
| `logins.py` copies | confirmed, 160, 175, 173 and 160 lines |
| 3 rigs hand-build the workflow worker | confirmed at `admission/tests/scan/conftest.py:326`, `admission/tests/service/conftest.py:288`, `dispatchapi/tests/control/conftest.py:196` |
| `psycopg.connect(` sites | 19 lines in `packages/*/src` on `main`, against the scout's 15 production sites: the extra four are the acceptance Postgres client, the migrate fixture, the dispatch smoke driver and the artifactclient bench, none a long-running service |
| free-port bind files | 16 in tests and deploy, 19 in the whole tree with the two helper modules and one more; the scout counted 18 |

One finding the scout did not make, which changes a candidate:

- C4: `scripts/affected.py:28-29` reads the workspace dependency graph from each package's `[tool.uv.sources]`, so scout candidate 4 ("remove 29 redundant `[tool.uv.sources]` blocks") would silently make `just pre-pr` skip the dependents of a changed package.
  Candidate 4 must not ship before `affected.py` reads `[project] dependencies`, or it must keep the blocks.
  It is a different lane from this epic, but this epic's adoption issues rely on the same blocks (section 3.3), so the captain should know before candidate 4 is filed.

Not checked:

- No test, cluster, container, `just` recipe or `nix develop` command was run, so every behavioural claim is from reading code.
  This includes that admission's connect hangs on a blackholed database (S2 reproduces it) and that `dsy_event_bytes` is exported on default buckets from admission and dispatch (ADM-3 and DIS-3 reproduce it).
- Whether any runbook other than `deploy/dispatchapi/README.md` names port 9090 for dispatch (H5 greps first).
- Whether `contracts/events/schemas` really are the draft the `Draft7Validator` pair assumes (scout, B C4, not relevant to this spec).
- Import cost of making `dsy_workflow.testing` a pytest plugin and its interaction with `--import-mode=importlib` (spike S1).
- Whether the reaper's payloads are plain dicts, so that the CLI's converter-less clients can move to `dsy_temporal.connect` (scout, A F8, checked in WFL-3).
- Whether a process-wide executor in `dsy_workflow.worker.executor()` can starve with two rigs alive in one pytest process (scout, B, open; RIG-W-2 runs the rigs together once to see).
- Live issue and pull request state beyond #464, #674, #679 and #682: whether the issue numbers cited in the scout report still hold.
- The shape of `docs/architecture/21-domains-and-access.md` and its diagram: they are written in L0-3, and the domain table of section 2.1 is its first draft.
- The exact `dsy_workflow.service.serve` line numbers (`:219-241`) are from this checkout, and WFL moves them.
