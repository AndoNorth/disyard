# Epic #9: Task placement scheduler (layer 2) - breakdown

Status: closed 2026-11-19
Epic: https://github.com/AndoNorth/weft/issues/9
Architecture note: docs/architecture/11-placement-and-workers.drawio, which 0.2 (issue) refines, and docs/architecture/02-two-layers-of-scheduling.drawio, unchanged
Verifier: none, per decision 1.
Depends on (closed epics): Dev environment (#1), Fixed-resource cluster (#2), Job event envelope (#4), DAG configuration schema and validator (#5), Durable state and artifact storage (#3), Artifact passing (#12), Workload toolchain (#11), Workflow engine (#6), Broker (#7), Kubernetes workers (#10), and the closed revisits #380 (the simulate slot) of #2 and #374 (the harness queue prefix) of #6
Depends on (open work): none. Admission (#8) is the active epic and keeps "no coupling in v1" with placement (`12-admission.md:43`, decision 13), and the dispatch API (#24) calls nothing of placement (`13-dispatch-api.md:363`, A9), so this epic opens when #8 closes and may run before, after or alongside #24, whose shared files with this epic are `scripts/cluster/test_budget.py` (section 5) and the `uv.lock` and root `pyproject.toml` hotspots, each under its rebase rule in section 3.

Evidence marks: `[read]` means read on `main` at `96a9123` on 2026-11-17 while preparing this spec, and nothing was executed beyond read-only commands and `scripts/cluster/budget.sh ci`, which reads files only.
`[review]` means read on `main` at `5146aa2` on 2026-11-18 while applying the captain's chosen findings of the spec review, and where a `[read]` fact and a `[review]` fact differ, the `[review]` one wins.
The epic issue body was read on 2026-11-17 and has no comments.
It predates the workflow engine and workers specs, so where it disagrees with landed code or a pinned contract, the code and the contract win, and section 1 lists each case.
Method: [docs/delivery/method.md](../method.md).
Model: [docs/delivery/epics/12-admission.md](12-admission.md) and [11-workers.md](11-workers.md), whose structure this spec follows.
Tiers: T0 is host Python with no runtime, T1 is containers started through `wft_itest` (the Temporal dev server of the pinned CLI image, at most two containers across the host), and T2 is the cluster from `just up` on preset `ci`.
A test that needs a container carries the `integration` marker and runs in `just itest` and in every lane's `just ci`.
Command prefixes `nix develop -c` are omitted from every command of this spec and every command takes them, and every Python command also takes `uv run --frozen`.
T2 commands run under `flock /tmp/wft-cluster.lock env WFT_CLUSTER_LOCK_HELD=1 ...` as the cluster epic says.
Every figure marked "estimate" is a proposal that a lane, spike S12 or #16 replaces with a measurement.

## 0. Decisions

Section 0.1 lists what is already decided and carried, including the captain's answers, 0.2 the questions for the captain, 0.3 the defaults that the lanes build on, and 0.4 the proposals that the issues pin.
Every decision in 0.1 and every default in 0.3 keeps every pinned contract as it is, except the replacement of `contracts/temporal/pools.yaml` that the contract itself assigns to this epic (decision 16).
A change to a recorded decision changes only what its Applied in column names, and a change to a default only what its Changes column names.
"0.1 (issue)" and "0.2 (issue)" name the lane 0 issues of section 4, and a bare 0.1 to 0.4 names a part of this section.

### 0.1 Recorded decisions

| # | Decision | Source | Applied in |
| --- | --- | --- | --- |
| 1 | No verifier skill, no feature files and no verify recipes. Every issue names its tests and the exit test is a command checklist | the verifier convention retired on 2026-11-15 (`docs/delivery/method.md:149-154`), carried by every executed epic (`11-workers.md:31`) [read] | sections 3, 4, 6 |
| 2 | Runtime libraries read contract files through package data and never by repository path, and each package has a wheel test | state epic decision 11, carried by every later epic (`12-admission.md:32`, decision 2) [read] | 0.2 (issue), every package issue |
| 3 | `place` is a local activity with the retry policy of `naming.yaml`'s local activities, which sets no attempt limit, so a placement service outage stalls a job and fails it only if the timer events of the outage exhaust the job's history budget, and its answer is recorded in history so a replay never asks again. A redeploy or a crash of the one placement pod is short against that budget; a long outage that fails jobs is #15's case to drive and #13's alert on the history length metric | `contracts/temporal/naming.yaml:34-36`, `08-workflow-engine.md:79` (D12), `:82` (D15) and `:83` (D16), `packages/workflow/tests/workflow/test_taskpath.py:87` [read] | W.1, D8, section 7 |
| 4 | The placer interface is `wft_workflow.placement.Placer` with `place(request, escalate=False) -> Placement \| Unschedulable`, the answer is the history payload `PlaceResult`, and `wft_workflow.testing.placer_contract(placer)` is the conformance test that this epic's client passes | `packages/workflow/src/wft_workflow/placement.py:21-64`, `testing/placer.py:71`, `08-workflow-engine.md:110,296,514` [read] | P.1, W.1, S.1 |
| 5 | The workflow owns every retry: a typed `Unschedulable` verdict fails the task, a schedule-to-start timeout means a new `place` call with the timeout doubled up to the ceiling with no count cap, and after `compile_oom`, `aggregate_oom` or two `oom_killed` in a row the workflow asks `place(request, escalate=True)` once and keeps the larger class only when `escalated` is true | `08-workflow-engine.md:53` (decision 22), `:70` (D3), `:72` (D5), `packages/workflow/src/wft_workflow/taskpath.py:239-242,533-542` [read] | decision 18, P.1 |
| 6 | Each pool has one unversioned queue, `wft-compile`, `wft-simulate` and `wft-aggregate`, and no build id appears in routing. This answers the epic's open question on queue naming during a rollout | `contracts/temporal/naming.yaml:15-19`, `11-workers.md:34` (decision 4) and `:65` (D3) [read] | 0.1 (issue), P.1 |
| 7 | Every pool runs one task per pod, one Deployment with `replicas: 1` each, so the slot count per pod and the replica count of the epic's pool table are constants | `11-workers.md:64` (D2), `deploy/presets/slots/ci.yaml:1` [read] | D4, S.1 |
| 8 | The real pool figures are the closure table of the workers epic | `11-workers.md:80` (D18) and `:545-555` [read] | decision 16, 0.1 (issue) |
| 9 | The placer answers with the contract queue names, and the workflow adds a job's queue prefix itself, so the placement service never sees a prefix | `11-workers.md:84` (D22), `taskpath.py:244-246` [read] | S.1 |
| 10 | The validator's resource check reads the slots files, and `pools.yaml` is held equal to the `ci` slots by a test, so the submit-time check and the unschedulable rule share one budget on `ci` | `contracts/job/README.md:72`, `contracts/job/errors.yaml:48-49`, `packages/workflow/tests/test_pools.py:33` [read] | decision 15, 0.1 (issue) |
| 11 | Under the default cgroup v2 group kill, an OOM in a pool pod reaches the workflow as `heartbeat_timeout`, and the failure scenarios epic (#15) decides `singleProcessOOMKill` | `11-workers.md:54` (Q3) and `:70` (D8) [read] | decision 18, section 7 |
| 12 | Workflow worker pods mount no service account token, which keeps Kubernetes credentials off workflow workers as the epic's key decisions ask | `deploy/workflow/Tiltfile.module:80` [read], the epic's key decisions | D7, D.1 |
| 13 | Admission keeps no coupling with placement in v1, and the layer 2 backlog numbers wait for #8's later backlog gate, as the epic's detail issue 6 says ("Later") | `12-admission.md:43` (decision 13), the epic body [read] | section 1 |
| 14 | Placement is on the proof-of-concept path after #10, which supplied the real pool values; the method's epic table still orders #9 (E9) before #10 (E11) and is stale against this answer | realignment answer D2 of 2026-11-13, `11-workers.md:32` (decision 2) [read] | section 1 |
| 15 | The unschedulable rule and the validator compare a task's declared memory with the pod slot (512 MiB on simulate) in this pass, section 7 records the gap that a task declaring 129 to 512 MiB passes submit and placement while the simulate guest gets 128 MiB (`packages/worker/src/wft_worker/pools/simulate.py:31`, `packages/runtime/src/wft_runtime/pins.py:11`, `11-workers.md:555`), and a usable-memory budget is left to #16's characterization, which sizes the slots. Rejected: a usable figure per pool read by the validator through a `revisit` of #5, which rewrites the example library (`memory_mib` 384 declared 1,146 times and 512 183 times [read]) and two closed epics for a cap #16 is expected to move; and a `revisit` of #10 capping the guest at the declared memory, which still accepts what cannot fit | captain | decision 10, sections 1 and 7 |
| 16 | The contract change of `contracts/temporal/pools.yaml`, which the contract assigns to this epic (`contracts/temporal/README.md:35`), keeps the schema and every number and sets `estimate: false`, and the README's pool table section is rewritten with the formulas and their measured terms: start-to-close is `run_timeout_s + start_to_close_margin_s`, where the 120 s margin covers the upload reserve (30 s, `simulate.py:34`), staging (up to 1.1 s) and the warm-up (33.2 s) of the closure table of #10, 64.3 s in all; the heartbeat timeout is at least three worker beats and kept at six of the 10 s beats (`packages/worker/src/wft_worker/activity.py:38`), because the beat loop shares a 250m CPU limit with the warm-up; the schedule-to-start timeout (its first value and ceiling in `pools.yaml`, the rule in `contracts/temporal/README.md:47`) stays #6's D5 rule, named in the README as the one figure #16 tunes. `VERSION` goes from 2 to 3, and `packages/workflow/tests/test_pools.py:41`, which asserts `estimate is True`, is edited in the same PR. Rejected: raising the first schedule-to-start timeout to the ceiling, which costs a 12 h alert latency; and no contract change, which leaves the flag claiming estimates for measured figures | captain | 0.1 (issue), 0.4, section 2 |
| 17 | The deployed workflow worker reaches the placement service through a `revisit` child of #6 (W.1): `ServicePlacer(url)` in `wft_workflow.placement`, the `place` activity made a plain function so the SDK runs it on a thread pool, since an `async def` calling a synchronous network placer (`placement.py:114-115`) would block the event loop, and an optional `WFT_PLACEMENT_URL` in the workflow service with `StaticPlacer` when it is absent; D.1 sets the variable in `deploy/workflow/Tiltfile.module`. A plain-function activity needs an `activity_executor` on every Worker that registers it, or the Worker refuses it at construction (`temporalio/worker/_activity.py:110-113`), and five workflow Workers are built by hand today with only the service passing one: the service (`service/__init__.py:175-180`), the harness T1 tier (`harness/__init__.py:443-447`), `tests/workflow/conftest.py:248-252`, `tests/versioning/test_versioning.py:149-153` and `tests/replay/record.py:146-150` [review]. So W.1 adds one shared builder of the workflow worker (0.4) that owns the executor, and the five sites call it. The client lives in `wft_workflow`, because the service needs its models and a client in the service's package would be a dependency cycle. Rejected: a required `WFT_PLACEMENT_URL`, which makes every T1 test and dev-server run of the workflow service need a placement service; the policy kept in the workflow worker, which departs from the epic's key decision that `place` asks the service; and `place` kept `async` with the blocking call moved onto a thread inside it, which keeps the five hand-built Workers diverging | captain | 0.4, D9, D10, W.1, D.1 |
| 18 | No pool offers a larger memory class in this pass: the default policy keeps `StaticPlacer`'s answer to `escalate=True`, the same class with `escalated: false` (decision 5), and the escalation stays a policy hook that a later policy fills. Rejected: a second simulate pool by memory class, which changes two pinned contracts (`naming.yaml` and the job registry) and two closed epics (#2, #10) for a path no `ci` run can reach before #15, where an OOM is not even reported as such (decision 11) | captain | D3, P.1, sections 1 and 7 |
| 19 | The pool snapshot decides nothing in this pass: the queue comes from the task type and the verdict from `pools.yaml`. It is read every 30 s (estimate), a failed read keeps the last good one with its age and the error, a placement never waits for it, and each answer is recorded in the service's decision log line with the snapshot's age and the ready workers of its pool. Nothing new enters the workflow history. Rejected: carrying the snapshot age and ready workers in the `Placement` answer, a second `revisit` change to #6's model; and answering `Unschedulable` for a pool with no ready worker, which is non-retryable and would fail tasks during an ordinary worker restart | captain | D5, D6, S.1 |

### 0.2 Questions

All questions are answered: the captain chose the recommended answer (a) of each, recorded as decisions 15 to 19.

### 0.3 Defaults the lanes build on

Numbers marked as estimates are fixed by a lane's measurement, S12 or #16.

| # | Topic | Default | Reason, and the alternative | Changes |
| --- | --- | --- | --- | --- |
| D1 | Package | One package `packages/placement/` (`wft_placement`, distribution `wft-placement`), depending on `wft-workflow` for the models, `StaticPlacer`, the contract loaders and `placer_contract`, which brings the chain that `11-workers.md:63` (D1) describes, and on nothing else new | One small service. The alternative, moving the placement models out of `wft-workflow`, changes a closed epic's package for image size only | 0.2 (issue), D.1 |
| D2 | Service API | `python -m wft_placement` serves one `http.server.ThreadingHTTPServer` on port 8080 with two routes: `POST /place` takes `PlaceCall` (`{request: PlacementRequest, escalate: bool}`) and answers 200 with `PlaceResult` as JSON, or 400 with a one-line reason for a body that does not parse; `GET /pools` answers the pool view of D4. Anything else is 404. The one service answers every live workflow build, and a second build runs beside the first during a rollout (`deploy/workflow/Tiltfile.module:1-3`), so a change to `PlacementRequest`, `Placement`, `Unschedulable`, `PlaceResult` or `PlaceCall` is additive only, a new field optional with a default, and rolls out with the placement image first and the workflow build that uses the field after it. Every model refuses an unknown field (`placement.py:17-18`), so a new answer field waits until no build without it is live, and a request the service cannot parse is a 400 that the local activity retries without end | The standard library server is what #24 chose (`13-dispatch-api.md:84`, D2), and no HTTP library is locked [read: `uv.lock` holds only `urllib3`]. The wire shape is the `wft_workflow.placement` models that both ends import, tested end to end by `placer_contract`, so no `contracts/placement/` directory is added. The alternative, a pinned contract with schema and vectors, buys a second check of a shape that both ends already import from one module, and the additive rule and the rollout order above are what keep the live builds and the service in step | P.1, S.1, W.1, D.1 |
| D3 | Policy | `wft_placement.policy.policy() -> Placer` returns `StaticPlacer()`, which reads the real `pools.yaml` after 0.1 (issue). Replacing the policy edits only `policy.py` | `StaticPlacer` already applies the unschedulable rules and the no-escalation answer of decision 18, and a second copy of those rules would drift | P.1 |
| D4 | Snapshot | Pure `wft_placement.snapshot.read(nodes, pods, topology) -> Snapshot` over the JSON of a node list and a pod list. Per task pool, the pools of `pools` in `contracts/temporal/naming.yaml`, with the pool label of `contracts/topology/topology.yaml`, whose `infra` and `obs` pools run no task and are left out of the snapshot and the view: the summed `allocatable` CPU and memory of the nodes labelled with the pool, the summed container requests of every pod on them that is neither `Succeeded` nor `Failed`, the difference, and the Ready pods labelled `app.kubernetes.io/name=<pool>-worker` with their images. The pool view of `GET /pools` adds the queue of `naming.yaml` and the row of `pools.yaml` to each pool | The worker pods carry that label (`deploy/workers/pool.star:48`), and the image tag carries the git short sha (`11-workers.md:65`, D3), which stands in for the epic's "build id" field. Init containers and pod overhead are left out of the requests, a ceiling marked in the code, because no pool pod has either today | P.1, K.1, S.1 |
| D5 | Cadence | The snapshot is read every 30 s (estimate) on a background thread. A failed read keeps the last good snapshot and records `age_s` and `last_error`; before the first good read the view shows `snapshot: null`. `POST /place` never waits for a read | decision 19. The epic asks that a read failure fall back to the last good snapshot and never block workflows | K.1, S.1 |
| D6 | Decision log | One plain line on stdout per answer: `placed type=<t> cpu_milli=<n> memory_mib=<n> escalate=<b> queue=<q> memory_class=<c> escalated=<b> snapshot_age_s=<n> ready_workers=<n>`, or `unschedulable type=<t> failure=<f> reason=<r> snapshot_age_s=<n> ready_workers=<n>`. Before the first good read both fields print `none`, and `ready_workers` also prints `none` for a task type with no pool. No tenant, job or task id, because the request carries none | decision 19. JSON logs belong to #13 | S.1 |
| D7 | Kubernetes access | A ServiceAccount `placement` in `wft-control`, a ClusterRole `wft-placement-read` with `get` and `list` on `nodes` and `pods`, and a ClusterRoleBinding. Only this pod mounts a token. The reader calls the API with the standard library (`urllib.request` and `ssl` with the mounted token and CA), lists and never watches | The epic's RBAC. Nodes are cluster scoped and pool nodes also run kube-system daemon pods, whose requests count, so the pod list is cluster wide. No Kubernetes client library is locked, and two list calls do not need one | K.1, D.1 |
| D8 | Deployment | Module `placement` (group `control`, namespace `wft-control`, profiles `dev`, `full` and `ci`, pin `python`), one replica with strategy `Recreate`, requests 50m and 128 MiB and limits 250m and 128 MiB (estimates, admission's figures), readiness through a ready file touched once the server listens, a ClusterIP Service `placement` on port 8080, no NodePort, no host port, no worker versioning and no build id. Image from `deploy/placement/{Dockerfile,image.sh}` in the way `deploy/workflow/` builds its own | The service is stateless and serves every workflow build, so it is unversioned, and D2's additive rule and rollout order keep it in step with the live builds. `Recreate` keeps the infra node at one pod, and `place` retries across the gap, which a redeploy keeps short against the history budget (decision 3) | D.1 |
| D9 | Client | `wft_workflow.placement.ServicePlacer(url, timeout_s=5)` posts `PlaceCall` with `urllib.request` and returns the parsed answer. A network error, a timeout or a status other than 200 raises, so the local activity retries under decision 3. 5 s is below the 30 s local start-to-close timeout (`taskpath.py:47`) | decision 17. A retry of a lost answer is harmless, because the policy is a pure function of the request | W.1 |
| D10 | Workflow wiring | The workflow service reads an optional `WFT_PLACEMENT_URL` into its `Config` and `ENV` of `service/__init__.py`, where the worker configuration lives (there is no config module): set, it builds its workflow worker with `ServicePlacer(url)`; absent, with `StaticPlacer()` as today. The `place` activity becomes a plain function, and every workflow worker, the service's and the four others of decision 17, is built by the builder of 0.4, which registers `place_activity(placer)` and runs it on the executor it owns | decision 17. The activity name and payloads stay, so a replay of an existing history is unchanged. The two admission test rigs that build a workflow worker (`packages/admission/tests/scan/conftest.py:326-336`, `tests/service/conftest.py:288-298`) already pass their own `activity_executor` and stay as built [review] | W.1, D.1 |
| D11 | Metrics | No metrics endpoint in this pass. Placement latency, decisions per pool, unschedulable count, escalations and snapshot age are readable from the decision log and the view, and #13 adds the metrics | The workers epic made the same choice (`11-workers.md:76`, D14), and the names belong to #13's catalogue | S.1, section 2 |
| D12 | Memory | S.1 measures the service's peak resident memory (`VmHWM` of `/proc/self/status`) at T0 over 1,000 `POST /place` calls and three snapshot reads from the fake API of K.1, and pastes it. A peak above 112 MiB (estimate, a margin under the 128 MiB limit) stops D.1 with a needs-decision status | The process imports `temporalio` and `pydantic` through `wft_workflow`, and admission set the same bar for the same limit (`12-admission.md:152`, D17) | S.1, D.1 |
| D13 | Budget | The module adds 50m and 128 MiB to the infra node: `scripts/cluster/budget.sh ci` goes from `2680m of 2900m, free 220m` and `5002Mi of 5504Mi, free 502Mi` to `2730m`, `free 170m` and `5130Mi`, `free 374Mi` (estimates, valid while the admission and dispatch modules keep their reserve figures), and D.1 edits the two asserting lines of `scripts/cluster/test_budget.py:42-43` and the infra memory total of `test_infra_node_of_5120_mib_is_over` at `:64`, which counts the module's full 128 MiB because `budget.sh` holds no `placement` reserve (`scripts/cluster/budget.sh:35-39`): `5002Mi of 4544Mi, OVER by 458Mi` becomes `5130Mi of 4544Mi, OVER by 586Mi` | [read: `budget.sh ci` output]. Admission's M.2 has landed and left these lines as they are (`test_budget.py:42-50`) [review]. Dispatch's D.2 moves the `dispatch` reserve line of the same test (`13-dispatch-api.md:410`), so whichever of D.2 and D.1 lands later rebases | D.1 |
| D14 | Queue partitions | Decided after S12: the pool queues keep the server's default of 4 partitions, and start order across jobs on a pool queue is approximate, as S12 measured: a task scheduled apart is overtaken by up to 51 of 200, siblings scheduled together by 146 to 157, and no task starved. Correctness never depends on queue order, because a task is ready only after every dependency is done (`settle` in `packages/workflow/src/wft_workflow/ready.py`), so build before run holds under any order. A batch finishes at the same time under either order, but the finish order of single jobs is not predictable | `deploy/temporal/` sets no partition key [read]. The alternative is one partition per pool queue, a Temporal dynamic config edit in #6's module (S12's control run set `matching.numTaskqueueReadPartitions` and `matching.numTaskqueueWritePartitions` to 1), which gives first-in-first-out between tasks scheduled in different workflow tasks and still leaves the sibling tasks of one workflow task unordered. Move to it when something consumes each job's result as it finishes, or when several submitters share the pools and need a fair share per job, and then rerun the S12 code with the setting and record the throughput, which is unmeasured. Open for whoever makes the switch: whether the setting can be scoped to one queue, and how to lower the count safely on a queue with a backlog | S12 |
| D15 | CI | No new CI workflow and no change to `.github/workflows/`. D.1 runs `scripts/tilt/run.sh ci` itself and pastes the output, and the exit test records a green run on `main` | The workers epic made the same choice (`11-workers.md:83`, D21) | D.1, section 7 |

### 0.4 Proposals the lanes pin

| Item | Proposal | Epic item |
| --- | --- | --- |
| Pool table | `contracts/temporal/pools.yaml` version 3 per decision 16, queue names unchanged in `naming.yaml` | detail issue 1 |
| Policy | `wft_placement.policy.policy() -> Placer` (D3) | detail issue 2 |
| Snapshot | `wft_placement.snapshot.Snapshot(taken_at, pools: dict[str, PoolCapacity])`, `PoolCapacity(nodes, allocatable_cpu_milli, allocatable_memory_mib, requested_cpu_milli, requested_memory_mib, ready_workers, images)`, and `read(nodes, pods, topology) -> Snapshot` (D4) | detail issue 4 |
| Reader | `wft_placement.kube.Reader(api_url, token_file, ca_file)` with `nodes()` and `pods()`, and `wft_placement.kube.Snapshotter(reader, every_s)` with `current() -> (Snapshot \| None, age_s, last_error)` (D5, D7) | detail issue 4 |
| Service | `python -m wft_placement` with configuration from the environment only: `WFT_PLACEMENT_LISTEN` (default `0.0.0.0:8080`), `WFT_SNAPSHOT_EVERY_S` (default 30), `WFT_READY_FILE`, and the in-cluster API address from `KUBERNETES_SERVICE_HOST` and `KUBERNETES_SERVICE_PORT`. A bad value exits 2, as the workflow service does | detail issue 4 |
| View | `python -m wft_placement.show [--url URL]` prints the pool view of `GET /pools`, one line per pool, `<pool> queue=<q> slot=<cpu>m/<mem>Mi ready_workers=<n> free=<cpu>m/<mem>Mi images=<list>`, then `snapshot_age_s=<n>` and `last_error=<e or none>` | detail issues 4, 9 |
| Client | `wft_workflow.placement.ServicePlacer(url, timeout_s=5)` and `PlaceCall` (D9) | detail issue 3 |
| Workflow worker builder | `wft_workflow.worker.workflow_worker(client, build_id, placer, emit, **options) -> Worker` in a new module `packages/workflow/src/wft_workflow/worker.py`: the Worker of the workflow queue with `JobWorkflow`, `place_activity(placer)` and `emit`, the versioned deployment of `build_id`, and as `activity_executor` one process-wide `ThreadPoolExecutor` it creates on first use, sized as the service's `local` pool is today (`THREADS`, `service/__init__.py:42`), with `max_concurrent_local_activities` equal to its thread count. Every other Worker option, `workflows` included, passes through `options`, and a caller never passes an executor (decision 17, D10) | detail issue 3 |
| Module | `deploy/placement/` with `module.yaml`, `Tiltfile.module` (ServiceAccount, ClusterRole, ClusterRoleBinding, Deployment, Service), `Dockerfile`, `image.sh` and render tests (D7, D8) | detail issue 4 |

### Rules agreed with epics that do not exist yet

These are stated so the owner epic confirms them in its own spec.

| Epic | Rule |
| --- | --- |
| #13 Observability | Defines and adds the placement metrics (latency, decisions per pool, unschedulable count, escalations, snapshot age) and the per-pool backlog and backlog age. The epic's request for "the serving build id as an info metric" becomes the worker image per pod that the view already shows, since no build id is in routing (decision 6) |
| #15 Failure scenarios | Drives the scheduler restart, a placement outage long enough to exhaust a job's history budget, which fails that job with `history-budget` (decision 3), a stale snapshot, a saturated pool, insufficient memory in its three shapes and the flood that must run every admitted task, through the harness and the commands of section 7 |
| #16 Performance and soak | Tunes the first schedule-to-start timeout and its ceiling (decision 16), the snapshot cadence (D5) and the usable-memory budget (decision 15), and checks the eventual-run condition: a task runs as long as its pool queue's wait stays below the 12 h ceiling, because each re-place moves it to the tail |
| #8 Admission | Its later backlog gate reads `DescribeTaskQueue` with `report_stats` per pool queue itself, or asks a later placement pass for it, since no consumer exists now (decision 13) |
| #17 Test ladder | The smoke's level 0 job is placed by the placement service on `ci` |
| #4 Envelope | The stage table row `placement` (`contracts/stages/stages.yaml:65-71`) says placement reads `task_type`, `cpu_cores` and `memory_mib` of `task.requested`, while the built path places from the plan task before that event exists and also needs `run_timeout_s`. This epic changes nothing in the stages contract and runs no stage contract, since placement reads no event; the owner may make the row exact in its next contract change |

## 1. Goal and non-goals

Goal: layer 2 scheduling picks the pool queue and the activity timeouts for each ready task, and nothing else.
A small stateless placement service in `wft-control`, reached by the workflow's `place` local activity, answers from a pure policy over the pool table, reads a read-only Kubernetes snapshot of the pools on a slow cadence, and records each decision with it.
Temporal holds the backlog and runs dispatch, retry and fencing, tasks wait in their pool queue first in, first out, and every admitted task must eventually run.

Non-goals and deferrals: the layer 2 backlog numbers from `DescribeTaskQueue` (later, with #8's backlog gate, decision 13), placement metrics (#13, D11), a second memory class and with it any real OOM escalation (decision 18), a usable-memory budget (#16, decision 15), the first schedule-to-start timeout as a measured figure (#16, decision 16), placement failure scenarios beyond section 7 (#15), and the `dev`, `full` and `soak` profiles running real pool workers.

### Breakdown inputs updated for what landed

The epic body is not edited.
The rows below that depart from it are confirmed by the captain's approval of this spec, which is the alignment step (`docs/delivery/method.md:11-12`).
At filing, the filer comments them on #9 with a link to this table.

| Epic body item | Now | Source |
| --- | --- | --- |
| Contract versions | events v4, job v2, activity v1, temporal v2 (v3 after 0.1), topology v1, stages v1, module v1, preset v1 | the `VERSION` files [read] |
| Pool table with "slot count per pod (one on `compile` and `aggregate`, several on `simulate`)", replicas and build id | One slot per pod and one replica on every pool, no build id in routing: constants in the view, the worker image standing in for the build | decisions 6, 7, D4 |
| "The values come from the pool manifests of #10, which are the source" | `pools.yaml` is a contract this epic sets, held equal to the `ci` slots preset, from #10's measured figures | decision 8, decision 16 |
| "The workflow calls `place`" with the scheduler as a separate service | The local activity calls the service through `ServicePlacer`, a revisit of #6 | decision 17, W.1 |
| The pool snapshot "recorded with each decision" | Recorded in the service's decision log line, not in history | decision 19, D6 |
| "A single OOM escalation ... in the next larger memory class, if one exists" | No larger class in this pass; on `ci` an OOM reaches the workflow as `heartbeat_timeout` | decision 18, decision 11 |
| "OOM is exit 137" (from #2's row) | The container's `memory.events`, and the cgroup v2 group kill | decision 11 |
| #4 row: placement reads CPU, memory and task type "from the data of the task request" | From the plan task, before the task request is built | #4 rule above |
| #5 row: the submit check "uses the same largest per-slot budget" | Already true on `ci` through the slots file and `test_pools.py` | decision 10, decision 15 |
| Layer 2 backlog numbers (detail issue 6) | Not in this pass | decision 13 |
| Placement metrics and alerts (detail issue 9) | Decision log and view now, metrics in #13 | D11 |

### Delivered already, and what is left

| Epic detail issue | Delivered by | Left for this epic |
| --- | --- | --- |
| 1 Pool table and task queue naming | `naming.yaml` queues (#6), unversioned (#10) | `pools.yaml` v3 (0.1) |
| 2 Placement function and policy interface | `Placer`, `PlacementRequest`, `Placement`, `Unschedulable`, `StaticPlacer` (#6) | the one-module policy (P.1) |
| 3 `place` local activity and decision record | the activity, its retry policy, the history record and the replay test (#6) | the client, a thread-pool activity and the workflow worker builder (W.1) |
| 4 Scheduler service and pool snapshot reader | nothing | P.1, K.1, S.1, D.1 |
| 5 Unschedulable verdicts and reasons | `StaticPlacer`, the placement rows of `failure-classes.yaml`, `placer_contract`, the validator's resource class (#6, #5) | reuse through D3 |
| 6 Layer 2 backlog numbers | nothing | later, decision 13 |
| 7 Pool queue ordering and no-starvation check | the re-place on a schedule-to-start timeout and its test (#6) | S12, exit rows 13 and 14 |
| 8 OOM escalation rule | the workflow side (#6) | none in this pass (decision 18) |
| 9 Placement metrics and alerts | nothing | decision log and view (S.1), metrics to #13 |
| 10 Placement failure scenarios | the scheduler-down retry and its test (#6, `scheduler_down_beyond_task_timeout`) | exit rows 11 and 12, the rest to #15 |

## 2. Interfaces

### Consumes (each pinned to a version)

| From | What | Pinned at | Stub used until it exists |
| --- | --- | --- | --- |
| Dev environment | Dev shell, `uv`, `kubectl`, `kind`, recipes `just test`, `just itest`, `just ci`, `just up`, `just down`, `just nuke`, `wft_itest` | `flake.lock`, `uv.lock` | none, exists |
| Cluster | Preset `ci`, slots, topology (pool label `wft.dev/pool`), namespaces and quotas, module contract, cluster lock, `budget.sh` | preset v1, topology v1, module v1 | none, exists |
| Envelope | `wft_events` models through `wft_workflow` | events v4 | none, exists |
| Validator | The job registry (task type to pool) and the slots-based resource check | job v2 | none, exists |
| Workflow engine | `Placer`, `PlacementRequest`, `Placement`, `Unschedulable`, `PlaceResult`, `StaticPlacer`, `place_activity`, `placer_contract`, the contract loaders, the naming contract, pool table v0, the harness with `--pools real` | temporal v2, activity v1, workflow v1 | none, exists |
| Workflow engine, revisit | `ServicePlacer`, `PlaceCall`, the thread-pool `place`, the workflow worker builder and `WFT_PLACEMENT_URL` (W.1) | not filed, a child of #6 filed at approval | `StaticPlacer` in the workflow worker until D.1 sets the variable |
| Workers | Three pool Deployments `<pool>-worker` in `wft-workers`, one slot each, their labels and images, and the closure figures | worker v1, `11-workers.md:545-555` | none, exists |

The names below were checked free on `main` [read: no `packages/placement/`, no `deploy/placement/`, no `docs/delivery/spikes/S12/`, no `ServicePlacer` in `packages/`].

### Produces (each pinned precisely)

| To | What | Where it lives (path) | Shape | Version |
| --- | --- | --- | --- | --- |
| the consumers of the Consumers table of `contracts/temporal/README.md` | Real pool table and timeout formulas | `contracts/temporal/pools.yaml`, `contracts/temporal/README.md` | schema unchanged, `estimate: false`, the formulas of decision 16 | temporal v3 |
| #6's workflow worker, #17, #15 | Placement service | `packages/placement/` (`wft_placement`), `deploy/placement/` | `python -m wft_placement`, routes of D2, module `placement` in `wft-control` (module v1) | placement v1 |
| #6 | Client and workflow worker builder | `wft_workflow.placement.ServicePlacer`, `PlaceCall`, `wft_workflow.worker.workflow_worker` | D9, passing `placer_contract` against the real service, and 0.4 | workflow v1, revised |
| #13, #15, #16 | Pool view and decision log | `GET /pools`, `python -m wft_placement.show`, the service's stdout | 0.4 View, D6 | placement v1 |
| #16, #15 | Queue order measurement | `docs/delivery/spikes/S12/queue-order.md` | S12's pass line and numbers | none |

### Assumptions about other epics

- #8 starts `JobWorkflow` with the options of `naming.yaml` and needs nothing from placement.
- #24 calls nothing of placement, and its validator call keeps the slots-based resource check.
- #10's workers keep their label `app.kubernetes.io/name=<pool>-worker` and one slot per pod.
- #6's `test_taskpath` and harness tests keep `StaticPlacer`, since T1 runs set no `WFT_PLACEMENT_URL`.
- #8's M.2 has landed, so whichever of #24's D.2 and this epic's D.1 edits `scripts/cluster/test_budget.py` later rebases onto the other.

## 3. Lanes

Package and module names are proposals.
Lane 0 owns every file it creates until the owning lane takes over the glob.

Departures from the method, stated so the review approves them knowingly:

- Lane 0 has two issues, each merged alone: 0.1 is the `contract-change` PR of `pools.yaml`, and it also edits `packages/workflow/tests/test_pools.py`, outside `contracts/`, because `test_pools_are_estimates_with_the_v0_timeouts` asserts the v0 flag and fails on the contract half alone (the precedent is #380, `11-workers.md:218`).
- Lane 0 carries no `just/*.just` module and adds no recipe: tests run through `just test`, `just itest` and `just ci`, and the service is a `python -m` command, as the workers and admission epics recorded.
- The revisit of #6 (W.1) is a child of #6 that runs in this epic's window, with its own glob in `packages/workflow/`.
- W.1 also edits files of #6, a closed epic, beyond the client: the four other hand-built workflow workers of decision 17 (the harness T1 tier and three test rigs) move onto the builder, and `packages/workflow/tests/test_placer_contract.py` runs the plain-function `place` without `asyncio.run`.
  The reason is that a plain-function `place` fails at construction on every Worker that passes no executor, and one builder that owns the executor removes that divergence where it starts, where an executor copied into four more Workers would keep five hand-built Workers drifting apart.
- D.1 edits `deploy/workflow/Tiltfile.module` and its render test, owned by #6, by one environment variable, and `scripts/cluster/test_budget.py`, owned by #2, by three lines (D13).
- S12 runs beside lane 0, or earlier beside #8, because it writes only under `docs/delivery/spikes/` and changes no product file.

| Lane | Workers | Owns (exclusive) | Issues | Needs |
| --- | --- | --- | --- | --- |
| 0 Contract and skeleton | 1 | 0.1: `contracts/temporal/{pools.yaml,README.md,VERSION}`, `contracts/temporal/vectors/pools/**`, `packages/workflow/tests/test_pools.py`. 0.2: `packages/placement/{pyproject.toml,README.md}`, `packages/placement/src/wft_placement/__init__.py`, `packages/placement/tests/{conftest,test_wheel}.py`, the two `wft-placement` lines of the root `pyproject.toml`, the `uv.lock` workspace entry, `docs/architecture/11-placement-and-workers.drawio` and its svg | 0.1, 0.2 | nothing (barrier, each merged alone) |
| P Policy and snapshot | 1 | `packages/placement/src/wft_placement/{policy,snapshot}.py`, `packages/placement/tests/policy/**` | P.1 | lane 0 |
| K Kubernetes reader | 1 | `packages/placement/src/wft_placement/kube.py`, `packages/placement/tests/kube/**` | K.1 | lane 0 |
| W Client (revisit of #6) | 1 | `packages/workflow/src/wft_workflow/{placement,worker}.py`, the `Config`, `ENV` and workflow worker of `packages/workflow/src/wft_workflow/service/__init__.py`, where the worker configuration lives (there is no config module), the workflow worker of `packages/workflow/src/wft_workflow/harness/__init__.py`, of `packages/workflow/tests/workflow/conftest.py`, of `packages/workflow/tests/versioning/test_versioning.py` and of `packages/workflow/tests/replay/record.py`, `packages/workflow/tests/test_placer_contract.py`, `packages/workflow/tests/{test_service_placer,test_worker}.py`, and the dated Closure addendum line of `docs/delivery/epics/08-workflow-engine.md` | W.1 | lane 0 |
| S Service | 1 | `packages/placement/src/wft_placement/{service,__main__,show}.py`, `packages/placement/tests/service/**` | S.1 | P.1, K.1, W.1 |
| D Image and module | 1 | `deploy/placement/**`, the `WFT_PLACEMENT_URL` entry of `deploy/workflow/Tiltfile.module` and its assertion in `deploy/workflow/test_render.py`, lines 42-43 and 64 of `scripts/cluster/test_budget.py` | D.1 | S.1 |
| Spike | 1 | `docs/delivery/spikes/S12/**`, the S12 row of `docs/delivery/spikes/README.md` | S12 | nothing |

Shared-file rules:

- `uv.lock` is the one hotspot, and 0.2 (issue) commits it with the workspace entry of `wft-placement` and its one dependency, `wft-workflow`, so no later lane edits `packages/placement/pyproject.toml`.
  A lock conflict is never resolved by hand, and `uv lock` is rerun after rebase.
- The root `pyproject.toml` changes only in 0.2 (issue), by one `wft-placement` line in `[tool.uv.sources]` and one in the dev dependency group.
- `packages/placement/tests/conftest.py` is lane 0's, and a lane keeps its own fixtures in a `conftest.py` inside its own test directory.
- `packages/workflow/src/wft_workflow/service/__init__.py` changes only in W.1, by the placer choice of D10 and the move of its workflow worker onto the builder.
- `packages/workflow/src/wft_workflow/harness/__init__.py`, `packages/workflow/tests/workflow/conftest.py`, `packages/workflow/tests/versioning/test_versioning.py` and `packages/workflow/tests/replay/record.py` change only in W.1, by the move of their workflow worker onto the builder, and `packages/workflow/tests/test_placer_contract.py` only by its two `place` calls and the `asyncio` import they leave unused.
- `deploy/workflow/Tiltfile.module` and `deploy/workflow/test_render.py` change only in D.1, by the one variable and its assertion.
- `scripts/cluster/test_budget.py` changes only in D.1, by the two node lines and the infra memory total of D13.
- `docs/delivery/spikes/README.md` gets one appended row from S12.
- `deploy/versions.yaml`, `deploy/presets/**`, `contracts/**` other than 0.1's paths, `.github/**`, `justfile`, `just/*.just`, `scripts/**` other than D13's lines, and every other package and `deploy/` directory are not touched.
- No tracked file holds a credential: the service's only credential is the mounted service account token.

## 4. Issues

One block per issue, in the [lane issue template](../../../.github/ISSUE_TEMPLATE/lane-issue.md), replaced by `#NNN` links after filing.
Done means, for every issue: `just ci` is green on the branch, the output of the named tests is pasted in the PR, and nothing outside Owns and the shared files changed.
An issue that names a T2 command runs it under the cluster lock and pastes its output.

### Mapping from the epic's detail issues

| Epic detail issue | Delivered by |
| --- | --- |
| 1 Pool table and task queue naming | 0.1 |
| 2 Placement function and policy interface | P.1 |
| 3 `place` local activity and decision record | W.1, on #6's activity |
| 4 Scheduler service and pool snapshot reader | K.1, S.1, D.1 |
| 5 Unschedulable verdicts and reasons | P.1, reusing `StaticPlacer` |
| 6 Layer 2 backlog numbers | not in this pass |
| 7 Pool queue ordering and no-starvation check | S12, exit rows 13 and 14 |
| 8 OOM escalation rule | P.1 keeps the no-escalation answer (decision 18) |
| 9 Placement metrics and alerts | S.1's decision log and view, metrics to #13 |
| 10 Placement failure scenarios | exit rows 11 and 12, the rest to #15 |

### 0.1 Real pool table

Lane 0, tier T0. Needs: nothing. Blocks: every other issue except S12. Label: `contract-change`.

- Scope: `contracts/temporal/pools.yaml` per decision 16, with its comments, the README's pool table section rewritten from "v0" to the real table with the formulas and their measured terms, `VERSION` from 2 to 3, the invalid pool vectors re-derived from the new pinned file, and `test_pools.py`'s v0 test renamed and changed to assert `estimate is False` and the same timeouts.
- Owns: 0.1's paths in lane 0.
- Consumes / produces: temporal v2.
  Produces temporal v3.
  Consumer issues to list in the PR: the open epics of the README's Consumers table (`contracts/temporal/README.md:77-86`), #8, #13, #15, #16 and #24 on `main` today [review].
- Acceptance criteria:
  - [ ] `contracts/temporal/check.sh contracts/temporal/pools.yaml` exits 0.
  - [ ] `pytest packages/workflow/tests/test_pools.py packages/workflow/tests/test_loader.py -v` passes, with `test_pools_match_ci_slots` and the renamed real-table test listed.
  - [ ] `pytest packages/workflow -v -k placer_contract` passes with `StaticPlacer` over the new file.
  - [ ] `cat contracts/temporal/VERSION` prints `3`, and `scripts/check-contracts-map.sh` exits 0.
- Verifier: `packages/workflow/tests/test_pools.py`, `test_loader.py`.

### 0.2 Package skeleton and diagram

Lane 0, tier T0. Needs: nothing. Blocks: P.1, K.1, W.1.

- Scope: the package with its wheel test, and the refinement of diagram 11: the placement service in `wft-control` with its read-only service account to the Kubernetes API, the `place` call from the workflow worker, the pool table file, the verdict from the table and the snapshot recorded beside it, OOM by `memory.events` with the group kill, and checkpoints as planned.
- Owns: 0.2's paths in lane 0.
- Consumes / produces: `wft-workflow`.
  Produces the package every lane builds in.
- Acceptance criteria:
  - [ ] `pytest packages/placement -v -k wheel` builds the wheel and imports `wft_placement` outside the repository.
  - [ ] `scripts/export-diagrams.sh --check` exits 0, and diagram 11 shows the placement service, its service account, the `place` call and the decision log, with no "exit code 137 plus a node event" and checkpoints marked planned.
- Verifier: `packages/placement/tests/test_wheel.py`.

### P.1 Policy and snapshot model

Lane P, tier T0. Needs: lane 0. Blocks: S.1.

- Scope: `wft_placement.policy.policy()` of D3 and the pure `wft_placement.snapshot.read` of D4.
- Owns: lane P's globs.
- Consumes / produces: temporal v3, topology v1.
  Produces the policy and the snapshot model that S.1 serves.
- Acceptance criteria:
  - [ ] `pytest packages/placement -v -k "policy_passes_placer_contract or policy_is_one_module or snapshot_sums_pool_nodes or snapshot_counts_daemon_requests or snapshot_skips_finished_pods or snapshot_ready_workers_and_images or snapshot_ignores_unlabelled_nodes"` passes.
  - [ ] `test_policy_is_one_module` shows that a policy module patched to answer a fixed queue changes the service's answers with no other module touched.
  - [ ] `test_snapshot_sums_pool_nodes` runs over node and pod list JSON fixtures shaped as `kubectl get nodes,pods -A -o json` prints them on a `ci` cluster, with the figures of D13's pools.
  - [ ] `test_snapshot_ignores_unlabelled_nodes` also holds an `infra` and an `obs` node, and the snapshot holds only the task pools of `naming.yaml` (D4).
- Verifier: `packages/placement/tests/policy/test_policy.py`, `test_snapshot.py`.

### K.1 Kubernetes reader

Lane K, tier T0. Needs: lane 0. Blocks: S.1.

- Scope: `wft_placement.kube.Reader` and `Snapshotter` of 0.4 with the standard library, the cadence and last-good rule of D5, and a fake API server in its tests, built on `http.server` with a self-signed certificate from `ssl` test helpers or plain HTTP behind a flag the tests set.
- Owns: lane K's globs.
- Consumes / produces: topology v1.
  Produces the reader that S.1 runs.
- Acceptance criteria:
  - [ ] `pytest packages/placement -v -k "reader_sends_token or reader_lists_pool_nodes or reader_lists_live_pods or snapshotter_keeps_last_good or snapshotter_reports_age_and_error or snapshotter_never_blocks_current"` passes.
  - [ ] `test_snapshotter_keeps_last_good`: after a good read the fake API fails three reads in a row, and `current()` keeps the first snapshot with `age_s` growing and `last_error` set, then clears the error on the next good read.
  - [ ] `test_reader_lists_pool_nodes` asserts the request path carries `labelSelector=wft.dev/pool`, read from `contracts/topology/topology.yaml` through package data and not typed in.
- Verifier: `packages/placement/tests/kube/test_reader.py`, `test_snapshotter.py`.

### W.1 Client and thread-pool `place` (revisit of #6)

Lane W, tier T0 and T1 (the dev server through `wft_itest`). Needs: lane 0. Blocks: S.1, D.1. Label: `revisit`, child of #6.

- Scope: `ServicePlacer` and `PlaceCall` of D9 in `wft_workflow.placement`, `place_activity` defining a plain function, the workflow worker builder of 0.4 and the five workflow workers of decision 17 moved onto it, `test_placer_contract.py`'s two `asyncio.run(ActivityEnvironment().run(place, ...))` calls made plain `ActivityEnvironment().run(place, ...)` calls, which return the value of a sync function (`temporalio/testing/_activity.py:230-232`), with the `asyncio` import they leave unused removed, the optional `WFT_PLACEMENT_URL` of D10 in the `Config` and `ENV` of `service/__init__.py`, and the dated Closure addendum line of `08-workflow-engine.md` linking this issue.
- Owns: lane W's globs.
- Consumes / produces: temporal v3.
  Produces the client and the builder that S.1 and D.1 use.
- Consumer epics affected, listed in the issue: the open epics #8 (its test rigs register `place_activity`), #15, #16 and #17 (the harness and its T1 tier).
- Acceptance criteria:
  - [ ] `pytest packages/workflow -v -k "service_placer_passes_placer_contract or service_placer_raises_on_refusal or service_placer_raises_on_timeout or place_is_not_a_coroutine or workflow_worker_owns_the_executor or service_uses_static_without_url or service_uses_service_placer_with_url"` passes, the first against an in-test `ThreadingHTTPServer` that answers with `StaticPlacer`.
  - [ ] `test_workflow_worker_owns_the_executor` builds a workflow worker with no executor argument and runs one job whose `place` runs on a thread of the builder's executor.
  - [ ] `pytest packages/workflow -q -rs` passes with no `SKIPPED` line, with the rigs of the five sites moved onto the builder and no assertion weakened: the PR's diff removes or changes no `assert` line, which shows that a thread-pool `place` keeps the history and the replay.
- Verifier: `packages/workflow/tests/test_service_placer.py`, `test_worker.py` and the existing workflow tests.

### S.1 Placement service

Lane S, tier T0. Needs: P.1, K.1, W.1. Blocks: D.1.

- Scope: `python -m wft_placement` with the routes of D2, the configuration of 0.4, the decision log of D6, the ready file, `python -m wft_placement.show`, and the memory measure of D12.
- Owns: lane S's globs.
- Consumes / produces: P.1, K.1, W.1.
  Produces the service that D.1 deploys.
- Acceptance criteria:
  - [ ] `pytest packages/placement -v -k "service_placer_contract_end_to_end or place_logs_one_line or place_does_not_wait_for_snapshot or bad_body_is_400 or pools_view_shape or show_prints_each_pool or config_refused_exits_2 or ready_after_listen"` passes, the first running `placer_contract(ServicePlacer(url))` against the real service in process with the fake API of K.1.
  - [ ] `test_place_does_not_wait_for_snapshot`: with the fake API holding every read for 10 s, 100 `POST /place` calls each answer within 1 s, and each log line prints `snapshot_age_s=none ready_workers=none` (D6).
  - [ ] `test_memory_peak` prints the `VmHWM` after 1,000 calls and three snapshot reads, and fails above 112 MiB (D12); the printed figure is pasted in the PR.
- Verifier: `packages/placement/tests/service/test_service.py`, `test_show.py`, `test_memory.py`.

### D.1 Image, module and workflow wiring

Lane D, tier T0 and T2. Needs: S.1. Blocks: the exit test.

- Scope: `deploy/placement/` of D7 and D8, the `WFT_PLACEMENT_URL` of the workflow Deployment set to `http://placement.wft-control.svc:8080`, and the budget lines of D13.
- Owns: lane D's globs.
- Consumes / produces: module v1, preset v1.
  Produces module `placement` on `ci`.
- Acceptance criteria:
  - [ ] `pytest deploy/placement deploy/workflow scripts/cluster/test_budget.py -v` passes, with render tests showing only the placement Deployment mounting a token, no NodePort, the ClusterRole holding only `get` and `list` on `nodes` and `pods`, and the workflow Deployment carrying `WFT_PLACEMENT_URL`.
  - [ ] `scripts/cluster/budget.sh ci` prints `free 170m` and `free 374Mi` on the infra node lines (D13), and `scripts/cluster/budget.sh dev` exits 0.
  - [ ] T2: `just up`, `scripts/tilt/run.sh ci`, then `kubectl --context kind-wft -n wft-control get pods -o wide` shows `placement` `Running` on the infra node, and `kubectl --context kind-wft -n wft-control logs deploy/placement` shows the server listening and a first snapshot read.
  - [ ] T2: `kubectl --context kind-wft -n wft-control exec deploy/placement -- python -m wft_placement.show` prints the three pools, each with `ready_workers=1`.
  - [ ] T2: `python -m wft_workflow.harness --kube-context kind-wft smoke --pools real --jobs 3` prints `finished: 3`, `failed: 0`, `verified: 3`, and the placement log holds a `placed` line for each placed task.
- Verifier: `deploy/placement/test_render.py`, `deploy/workflow/test_render.py`, `scripts/cluster/test_budget.py`, and the T2 commands above.

### S12 Pool queue order

Spike, tier T2. Needs: nothing. Blocks: nothing; its result informs D14, #15 and #16. Timebox: 1.5 days.

- Question: on the `ci` cluster's Temporal, with the server's default partitions, in what order does one single-slot worker start activities scheduled one after another on one pool-shaped queue, and does every one start?
- Method: throwaway code in `docs/delivery/spikes/S12/code/` starts a measurement workflow on its own queue that schedules 200 activities, numbered, on a fresh queue `s12-<hex>-wft-simulate` with no worker, then starts one worker with `max_concurrent_activities=1` that records the start order, through a `kubectl port-forward` bound to 127.0.0.1, and never touches the contract queues.
- Pass line: 200 of 200 activities start within 10 minutes, and no activity starts after more than 50 activities scheduled later than it (a quarter of the run).
- Result: `docs/delivery/spikes/S12/queue-order.md` with pass or fail, the start order's inversion count and the largest overtaking, and the decision it settles: on a fail, a question to the captain whether the pool queues get one partition through #6's Temporal module.
- Owns: the spike lane's globs.

## 5. Order, stubs and spikes

- First, in parallel: 0.1 and 0.2 prepared together and merged one at a time, and S12, which may run earlier beside #8.
  0.1 and 0.2 merge in either order, each alone, with an estimate of 1.5 days for the pair.
- After lane 0: P.1, K.1 and W.1 in parallel.
- After P.1, K.1 and W.1: S.1.
- After S.1: D.1.
- Then the exit test.
- DAG: {0.1, 0.2} then {P.1, K.1, W.1} then S.1 then D.1 then the exit test, with S12 off the path.
- Cluster work: S12, D.1's T2 rows and the exit test, each under the cluster lock, one at a time.
- Overlap with #24: if this epic opens while #24 is active, both epics edit `scripts/cluster/test_budget.py`, owned by #2, a product file the method lets no two epics share without a reason stated here (`docs/delivery/method.md:232-233`).
  The reason is that the two edits are figures of one budget check on different lines, D.1's lines 42-43 and 64 and #24's `dispatch` reserve line (`13-dispatch-api.md:410`), so whichever lands later rebases onto the other, and this epic does not wait for #24 to close.
  Both epics also edit the `uv.lock` and root `pyproject.toml` hotspots, each under its rebase rule in section 3.

Stubs:

| Stub | What it fakes | How its conformance test runs against the real thing |
| --- | --- | --- |
| `StaticPlacer` (existing) | the placement service, in the workflow worker until D.1 sets `WFT_PLACEMENT_URL`, and in every T1 run of #6 | `placer_contract` runs against `StaticPlacer` (0.1), against `ServicePlacer` over an in-test server (W.1) and against `ServicePlacer` over the real service (S.1) |
| The fake API server of K.1 | the Kubernetes API, for K.1 and S.1 | D.1's T2 `show` row and exit row 9 read the real API with the same reader |
| The in-test placement server of W.1 | the placement service, for W.1 | S.1 runs the same `placer_contract` against the real service |

Spikes: S12, as its block in section 4 states, gates no lane.

## 6. Test delta

Tests: each issue in section 4 names its tests and tier, T0 under `packages/placement/tests/` and `deploy/placement/`, T0 and T1 under `packages/workflow/tests/` for 0.1 and W.1, all run by `just test`, `just itest` and `just ci`.
Commands: `python -m wft_placement`, `python -m wft_placement.show`, `deploy/placement/image.sh`.
No helper script, verifier skill, feature file or verify recipe (decision 1).
What stays untested is listed under "Not provable by this epic" in section 7.

## 7. Exit test

Run by a worker who did not write the lanes, on a clean `git archive` export of `main` taken outside any git repository, on the 20 GiB WSL2 host with Docker running for the whole run.
Containers other than the ones the run starts are never touched.
Each command runs from the export root, and its real output is the evidence file `exit-test/row-<#>.txt` beside the export, pasted in the PR that records the run and summarised in Closure.
The export has no git checkout, so row 7 sets `WFT_BUILD_ID=exit-a`, as the workers exit run did (`11-workers.md:511`).
Rows 7 to 16 take the cluster lock for their whole sequence, and row 17 runs outside it.
`H` stands for `python -m wft_workflow.harness --kube-context kind-wft`, and `K` for `kubectl --context kind-wft`.
Rows 11 and 12 replace the placement pod, and its decision log and cgroup counters go with it, so `P <name>` saves the current pod's reading before it is replaced: `K -n wft-control logs deploy/placement > exit-test/<name>.log`, `K -n wft-control exec deploy/placement -- cat /sys/fs/cgroup/memory.events > exit-test/<name>.events` and `K -n wft-control get pods -l app.kubernetes.io/name=placement -o jsonpath='{.items[*].status.containerStatuses[*].restartCount}' > exit-test/<name>.restarts`.

| # | Command | Expected |
| --- | --- | --- |
| 0 | `grep -E '^(MemTotal\|MemAvailable):' /proc/meminfo` and `docker ps --format '{{.Names}}'`, repeated just before row 7 | `MemAvailable` at least the `memory_mib` of `deploy/presets/ci.yaml` at the run's commit plus 1,024 MiB, in kB (13,369,344 kB at 12,032 MiB), else the run stops and records the figure |
| 1 | `uv sync --frozen` then `uv lock --check` | exit 0 for both, lock unchanged |
| 2 | `pytest packages/placement -q -rs` | passes with no `SKIPPED` line |
| 3 | `pytest packages/placement packages/workflow deploy/placement deploy/workflow -v -k "placer_contract or policy_is_one_module or snapshot_ or snapshotter_ or service_placer or place_is_not_a_coroutine or workflow_worker_owns_the_executor or place_does_not_wait_for_snapshot or memory_peak or render or pools"` | each named test listed `PASSED`, `placer_contract` against `StaticPlacer`, the in-test server and the real service |
| 4 | `pytest packages/workflow -q -rs` | passes with no `SKIPPED` line, so the revisit left #6's tests green |
| 5 | `contracts/temporal/check.sh contracts/temporal/pools.yaml` and `cat contracts/temporal/VERSION` | exit 0, and `3` |
| 6 | `scripts/check-contracts-map.sh`, `scripts/export-diagrams.sh --check`, `just versions --check`, `scripts/cluster/budget.sh ci`, then diagram 11 opened | exit 0 for each, the infra node lines `free 170m` and `free 374Mi` (D13), and the diagram showing the placement service, its service account and the `place` call |
| 7 | `just up`, `WFT_BUILD_ID=exit-a scripts/tilt/run.sh ci`, `K -n wft-control get pods -o wide`, `K -n wft-control logs deploy/placement` | exit 0, `placement` `Running` on the infra node, and its log showing the listener and a first good snapshot |
| 8 | `K auth can-i list pods --all-namespaces --as=system:serviceaccount:wft-control:placement`, the same for `list nodes`, `create pods` and `get secrets -n wft-control`, then `K auth can-i list nodes --as=system:serviceaccount:wft-control:default` | `yes`, `yes`, `no`, `no`, then `no` |
| 9 | `K -n wft-control exec deploy/placement -- python -m wft_placement.show`, run at least one snapshot cadence (30 s) after row 7, so the snapshot read is newer than the last pool worker turning Ready | three lines, `compile`, `simulate` and `aggregate`, each with its contract queue, its slot from `pools.yaml`, `ready_workers=1` and one image, and `snapshot_age_s` at most 60 |
| 10 | `H smoke --pools real --jobs 10`, then `K -n wft-control logs deploy/placement \| grep -c '^placed '` | `finished: 10`, `failed: 0`, `verified: 10`, then at least 40 (the four simulate tasks of each job), `60` in the run of 2026-11-19, where each job also placed its `aggregate` task and its `compile` task missed the compile cache, which skips `place` on a hit |
| 11 | `P pod-a`, `K -n wft-control scale deploy/placement --replicas=0`, then `( sleep 120; K -n wft-control scale deploy/placement --replicas=1 ) & H smoke --pools real --jobs 3` | the reading of the pod that served rows 7 to 10 saved, then `finished: 3`, `failed: 0`: a placement outage of two minutes, beyond the local retry threshold of 60 s, stalls the jobs and fails none |
| 12 | `( sleep 20; P pod-b; K -n wft-control delete pod -l app.kubernetes.io/name=placement --grace-period=0 --force ) & H smoke --pools real --jobs 10` | the reading of the pod that served row 11 saved just before the kill, `finished: 10`, `failed: 0`, and a new `placement` pod `Running` afterwards |
| 13 | `grep -c '^unschedulable ' exit-test/pod-a.log exit-test/pod-b.log`, then `K -n wft-control logs deploy/placement \| grep -c '^unschedulable '` | `0` for each of the three pods: no task of rows 10 to 12 was refused |
| 14 | the count command of row 10, then `H smoke --jobs 20`, then the count command of row 10 and of row 13 again | on fake pools behind a fresh prefix, `finished: 20`, `failed: 0`, the second `placed` count at least 80 above the first on the same pod, since the deployed workflow worker places a prefixed job through the same service on the contract names, and the `unschedulable` count still `0` |
| 15 | `P pod-c`, `cat exit-test/pod-a.events exit-test/pod-b.events exit-test/pod-c.events exit-test/pod-a.restarts exit-test/pod-b.restarts exit-test/pod-c.restarts`, then `K -n wft-control exec deploy/placement -- cat /sys/fs/cgroup/memory.stat` | `oom_kill 0` and a restart count of `0` for each of the three pods, and `anon` recorded in Closure against D12's 112 MiB |
| 16 | `just doctor`, then `just down`, then `just nuke` twice | exit 0 for each, and the second `just nuke` deletes nothing |
| 17 | `just ci` | exit 0, on the full path |
| 18 | the `ci` and `state-modules` workflows on `main` | one green run of each, the URLs recorded in Closure |

Not provable by this epic: an OOM escalation into a larger memory class and OOM classification at the pod limit (decision 18, #15), a usable-memory budget that refuses at submit a simulate task needing more than the 128 MiB guest cap (decision 15, #16), the eventual run of every task under a backlog whose wait passes the 12 h schedule-to-start ceiling (#16), strict first-in-first-out order across queue partitions (approximate by D14), the layer 2 backlog numbers (#8's later gate), placement metrics and alerts (#13), a stale snapshot over a long API outage, a saturated pool under load and a placement outage long enough to fail a job through its history budget (#15, decision 3), the placement and validator budgets on the `dev` and `soak` presets, whose simulate slot is 384 MiB while `pools.yaml` holds the `ci` 512 MiB, and the `dev`, `full` and `soak` profiles with real pool workers.

## 8. Parallelism and cost

- Maximum parallel workers: 2 in the first phase (lane 0's author and S12), then 3 (P.1, K.1, W.1), then 1 for S.1, D.1 and the exit test.
- Host constraints: W.1's T1 tests hold one dev server, within the two-container limit of `wft_itest`.
  The cluster is single-owner, so S12, D.1's T2 rows and the exit test queue for the lock, and `ci` needs 12,032 MiB of the 20 GiB host beside other containers (`deploy/presets/ci.yaml:8`).
- CI cost, estimates: no new workflow, and the new T0 tests run in seconds; W.1 adds one dev server session to `just itest`.
- Estimates, not measurements, in worker-days: 0.1 (0.5), 0.2 (1), P.1 (1), K.1 (1.5), W.1 (1.5), S.1 (2), D.1 (2.5), S12 (1.5), exit test (1).
  That is about 12.5 worker-days of effort and about 8.5 days elapsed along lane 0, K.1 or W.1, S.1, D.1 and the exit test (1.5 + 1.5 + 2 + 2.5 + 1).
  The main sources of slip are the image build of a `wft-workflow` chain on the infra node's 128 MiB limit (D12), RBAC and TLS details of the in-cluster API read with the standard library, rebases of `scripts/cluster/test_budget.py` against #24, and cluster lock contention.

## 9. Closure

Run on 2026-11-19 against `45e731878726af1315f4d8ac64a6849d84bb8c95`, the merge of D.1 and the last lane merge, from a `git archive origin/main` extracted into a scratch directory with no enclosing git repository.
Every row passes, row 9 on its second reading and rows 9 and 10 as their wording is corrected in section 7.
The pasted output of every row is in the pull request that records this run.

Method of the run:

- The worker that ran it wrote none of the lanes.
- The host is the 20 GiB WSL2 VM with Docker running for the whole run.
  The one container not started by the run (`open-webui`) was left untouched.
- Rows 0 to 6 ran first in order, rows 7 to 16 ran in one sequence under the cluster lock (`flock /tmp/wft-cluster.lock env WFT_CLUSTER_LOCK_HELD=1 bash <script>`) with row 0 repeated just before row 7, and rows 17 and 18 ran after the lock was released.
- A stopped `wft` cluster of preset `ci`, left by D.1's T2 rows, and its `wft-registry` were removed with `just nuke` under the lock before row 0's repeat, so row 7 started from no cluster and the harness made `exit-a` current itself.
- The export has no git checkout, so row 7 runs `scripts/tilt/run.sh ci` with `WFT_BUILD_ID=exit-a`, as the section says.
- Each harness row ran as `timeout -s INT 1800 python -m wft_workflow.harness ...`, a 30 minute cap that no row came near.
- Rows 4 and 17 ran with `WFT_SLOT=2`, the pool slot of the worktree the run was launched from, so their T1 session could not share ports with a sibling's ([#466](https://github.com/AndoNorth/weft/issues/466)).
- Row 6's diagram was rendered to PNG with ImageMagick and read by eye.
- A memory sampler read `MemAvailable` every 5 s through rows 7 to 16.

Results:

| # | Result | Evidence |
| --- | --- | --- |
| 0 | pass | `MemAvailable` 17,973,760 kB at the start and 18,035,288 kB just before row 7, both above the gate of 13,369,344 kB (`memory_mib` 12,032 of the `ci` preset plus 1,024 MiB) |
| 1 | pass | `uv sync --frozen` exit 0, `uv lock --check` exit 0 with 75 packages resolved, the `uv.lock` SHA-256 unchanged |
| 2 | pass | 54 passed in 20.5 s, no `SKIPPED` line |
| 3 | pass | 80 passed and 398 deselected, each named test `PASSED`: `placer_contract` as `test_static_placer_passes_placer_contract` (`StaticPlacer`), `test_service_placer_passes_placer_contract` (the in-test server), `test_service_placer_contract_end_to_end` (the real service) and `test_policy_passes_placer_contract`, with `test_memory_peak`, `test_place_is_not_a_coroutine`, `test_workflow_worker_owns_the_executor`, `test_place_does_not_wait_for_snapshot`, the `snapshot_`, `snapshotter_`, `pools` and render tests |
| 4 | pass | 404 passed in 8 min 38 s with `WFT_SLOT=2`, no `SKIPPED` line |
| 5 | pass | `ok contracts/temporal/pools.yaml` and `3` |
| 6 | pass | exit 0 for the four commands, the infra node lines `2730m of 2900m, free 170m` and `5130Mi of 5504Mi, free 374Mi`, and diagram 11 shows the placement service in `wft-control`, its read-only service account listing nodes and pods every 30 s, and the `place` and `verdict` arrows from the workflow worker |
| 7 | pass | `just up` in 46 s, `SUCCESS. All workloads are healthy.` after 185 s, `placement` `Running` on `wft-worker`, the node of pool `infra`, beside `admission`, `dummy-control` and `workflow-worker-exit-a`, and the log `listening address=http://0.0.0.0:8080 snapshot_every_s=30.0` then `snapshot first_read=true ready_workers=compile:0,simulate:0,aggregate:0` |
| 8 | pass | `yes`, `yes`, `no`, `no`, then `no` |
| 9 | pass on the second reading, as corrected | the first reading, 1 s after Tilt's healthy line, printed `simulate ... ready_workers=0 ... images=none` with `snapshot_age_s=23`, because the simulate worker turned Ready at 05:29:05Z, after the snapshot was read; the second, at 05:29:39Z, printed the three pools on `wft-compile`, `wft-simulate` and `wft-aggregate` with slots `1000m/1024Mi`, `250m/512Mi` and `1000m/1024Mi`, `ready_workers=1` and one image each, `snapshot_age_s=5` and `last_error=none` |
| 10 | pass, as corrected | `finished: 10`, `failed: 0`, `verified: 10` in 377 s, then `60`: 10 `compile`, 40 `simulate` and 10 `aggregate` lines |
| 11 | pass | pod-a saved from `placement-848dbb5774-6hjw6`, the deployment at 0 replicas from 05:35:25Z to 05:37:25Z, `finished: 3`, `failed: 0`, `verified: 3` in 154 s, and a new pod `Running` |
| 12 | pass | pod-b saved from the pod that served row 11 20 s into the smoke, then force deleted, `finished: 10`, `failed: 0`, `verified: 10` in 39 s, and a new `placement` pod `Running` with 0 restarts |
| 13 | pass | `0` for pod-a, pod-b and the live pod |
| 14 | pass | `7` before, `finished: 20`, `failed: 0`, `queue_prefix: h-a94cd0b6-` in 18 s, `124` after on the same pod (117 above the first), and the `unschedulable` count still `0`; every `queue=` of the three pods' logs is a contract name (27 `wft-compile`, 172 `wft-simulate`, 43 `wft-aggregate`) |
| 15 | pass | `oom_kill 0` (and `oom 0`, `max 0`) in each of the three `memory.events`, a restart count of `0` for each pod, and `anon 43028480` |
| 16 | pass | exit 0 for `just doctor`, `just down` and both `just nuke`, the second deleting no node, then `No kind clusters found.` with only `open-webui` running |
| 17 | pass | `just ci` exit 0 in the export with `WFT_SLOT=2`, `== ci path: full`, 3,194 passed in `just test` in 15 min 54 s and 766 passed in `just itest` in 37 min 44 s, and `nix flake check` passed, 54 min 7 s in all |
| 18 | pass | the `ci` run [44463425981](https://github.com/AndoNorth/weft/actions/runs/44463425981), success on `45e7318` on `main`, full path, 3,194 passed in `just test` and 766 in `just itest`, and the `state-modules` run [44463425903](https://github.com/AndoNorth/weft/actions/runs/44463425903), success on `45e7318`; no run on `main` holds this record yet, since it is the pull request that adds it |

Row 9's wording: as first written, the row ran straight after row 7, but Tilt reports healthy as soon as the last pool worker is Ready, and the snapshot is read every 30 s (D5), so the view can show a pool's worker as not ready for up to one cadence.
The row now runs at least one cadence after row 7, and the check that matters, `ready_workers=1` from the real API through the same reader, is unchanged.
Row 10's wording: as first written, the row counted only the four simulate tasks of each job.
The real count is 60, since each job also places its `aggregate` task and a `compile` task that misses the compile cache (`taskpath.py:310`), which skips `place` on a hit; rows 11 and 12 placed no `compile` task for that reason.
The row keeps its floor of 40 and states the 60 of this run.

Memory figures:

- Host: `MemTotal` 20,482,272 kB, `MemAvailable` 18,035,288 kB at the gate before row 7, a low of 14,694,768 kB at 05:29:45Z as row 10 started, and 18,061,144 kB after row 16.
- Placement (D12): S.1 measured `VmHWM` 62.7 MiB after 1,000 calls and three snapshot reads at T0, and row 15 found `anon` 43,028,480 bytes (41.0 MiB) in the pod that served rows 12 to 14, against D12's 112 MiB and the 128 MiB limit, with no OOM in any of the three pods.
- The `ci` infra node: `budget.sh ci` printed D13's `free 170m` and `free 374Mi`.

What was learned:

- Placement answered every task of 43 jobs on three pods with no `unschedulable` line, on contract queue names only, including the 20 prefixed jobs of row 14 on fake pools, so the workflow alone adds the prefix (decision 9).
- A placement outage of 120 s stalled three jobs and failed none, and a forced deletion mid-smoke cost no job: the local activity's retry carried both (decision 3).
  Row 4's T1 output puts a 127 s outage at 27 history events and an hour at 75.
- The snapshot never decided anything and its staleness showed only in the view: row 9's first reading lagged a worker by under one cadence, as decision 19 intends, and the largest `snapshot_age_s` logged on a placement was 30.
- The compile cache lookup runs before `place`, so the number of placements per job depends on the cache, which a count-based check has to allow for.
- S12 failed its pass line at the server's default of 4 partitions (`docs/delivery/spikes/S12/queue-order.md`), and the captain decided D14 as approximate order at the default: the pool queues keep 4 partitions, and one partition per pool queue through #6's Temporal module is the switch D14 names, with its triggers.
  No row depends on the order, and strict order stays under "Not provable" in section 7.

What the architecture note now shows: `docs/architecture/11-placement-and-workers.drawio` and its SVG are as 0.2 (issue) left them, and were re-checked by eye in this run for row 6.

Which assumptions of section 2 held: all five held on `main` at `45e7318`.
#8 closed with its workflow start unchanged, and admission ran beside placement in rows 7 to 16; `packages/dispatchapi/` on `main` at `bedad93`, #24's D.1, names nothing of placement; the pool workers keep `app.kubernetes.io/name=<pool>-worker` (`deploy/workers/pool.star:48`), which row 9 read; row 4 ran #6's T1 tests on `StaticPlacer` with no `WFT_PLACEMENT_URL`; and D.1 landed after #8's M.2 with `test_budget.py` passing in row 17.

What moved to which epic, unchanged from section 7:

- an OOM escalation into a larger memory class and OOM classification at the pod limit (decision 18, #15)
- a usable-memory budget that refuses at submit a simulate task needing more than the 128 MiB guest cap (decision 15, #16)
- the eventual run of every task under a backlog near the 12 h schedule-to-start ceiling (#16)
- strict first-in-first-out order across queue partitions (approximate by D14, with the switch and its triggers there)
- the layer 2 backlog numbers (#8's later gate)
- placement metrics and alerts (#13)
- a stale snapshot over a long API outage, a saturated pool, and a placement outage long enough to fail a job through its history budget (#15)
- the placement and validator budgets on the `dev` and `soak` presets, and the `dev`, `full` and `soak` profiles with real pool workers

Follow-ups found:

- [#512](https://github.com/AndoNorth/weft/issues/512): the broker's `test_counters` failed once on `main` on an empty DLQ pull after the last retry, in the `ci` run of S.1's merge, outside this epic.
- D14 is decided as approximate order at the default of 4 partitions, not filed.

Closure addenda, one dated line each, added after close when a `revisit` child issue is filed against this epic:

- 2026-11-21: [#561](https://github.com/AndoNorth/weft/issues/561) serves the placement metrics on 9464 and sets up `wft_obs` with the decision line kept as the `msg` of its JSON log line, with `obs_env()` in the module Tiltfile.
