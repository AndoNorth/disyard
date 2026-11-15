# Epic #10: Kubernetes workers - breakdown

Status: draft
Epic: https://github.com/AndoNorth/wasm-dag-forge/issues/10
Architecture note: docs/architecture/11-placement-and-workers.drawio, and the worker detail in a new diagram `docs/architecture/NN-pool-worker.drawio`, which 0.1 (issue) adds with the next free number on `main`, because #8's spec has taken 17 (`docs/architecture/17-admission-cycle.drawio`) and #24's names its own diagram `NN`
Depends on (closed epics): Dev environment (#1), Fixed-resource cluster (#2), Job event envelope (#4), DAG configuration schema and validator (#5), Durable state and artifact storage (#3), Artifact passing (#12), Workload toolchain (#11), Workflow engine (#6)

Evidence marks: `[read]` means read on `main` at `6439283` on 2026-11-15 while preparing this spec, and nothing was executed beyond read-only commands.
`[looked up]` means read on 2026-11-15 from upstream documentation, and nothing was run against a cluster.
The epic issue body was read on 2026-11-15, and it has no comments.
The body predates the workflow engine spec, so where it disagrees with landed code or a pinned contract, the code and the contract win, and section 1 lists each case.
Method: [docs/delivery/method.md](../method.md).
Model: [docs/delivery/epics/08-workflow-engine.md](08-workflow-engine.md), whose structure this spec follows.
Tiers: T0 is host Python with no runtime, T1 is containers started through `wdf_itest` (the Temporal dev server of the pinned CLI image and SeaweedFS, at most two containers across the host) plus the real `wasmtime`, `wasm-tools` and `componentize-py` of the dev shell, and T2 is the cluster from `just up` on preset `ci`.
A test that needs a container carries the `integration` marker and runs in `just itest` and in every lane's `just ci`.
Command prefixes `nix develop -c` are omitted from every command of this spec and every command takes them, and every Python command also takes `uv run --frozen`.
T2 commands run under `flock /tmp/wdf-cluster.lock env WDF_CLUSTER_LOCK_HELD=1 ...` as the cluster epic says.
Every figure marked "estimate" is a proposal that a spike, a lane or #16 replaces with a measurement.

## 0. Decisions

Section 0.1 lists what is already decided and carried, 0.2 the questions the captain decided, 0.3 the defaults that the lanes build on, and 0.4 the proposals that the issues pin.
A change to a recorded decision changes only what its Applied in column names, and a change to a default only what its Changes column names.
Inside section 0, "0.1 (issue)" names the lane 0 issue of section 4 and a bare 0.1 to 0.4 names a part of this section.
From section 1 on, a bare 0.1 names that issue, and 0.2 to 0.4 still name parts of this section.

### 0.1 Recorded decisions

| # | Decision | Source | Applied in |
| --- | --- | --- | --- |
| 1 | No verifier skill, no feature files and no verify recipes for this epic. Every issue names its tests and the exit test is a command checklist | the standing rule of every executed epic (`08-workflow-engine.md:32`), and the realignment answer of 2026-11-13 (D9) that retires `verify` and `verify-clean` | sections 3, 4, 6 |
| 2 | Placement (#9) is on the proof-of-concept path and runs after this epic, which supplies the real pool values that #9 turns into `contracts/temporal/pools.yaml`. The method's epic table still orders #9 (E9) before this epic (E11), and is stale against this answer | realignment answer of 2026-11-13 (D2), against `docs/delivery/method.md:173,175` [read] | D18, section 2 |
| 3 | The e2e on `ci` drops the two-compile-node check, so this epic deploys one compile pod and nothing asks for two | realignment answer of 2026-11-13 (D3), `deploy/presets/ci.yaml:23-26` [read] | D2, section 2 |
| 4 | Every pool queue is unversioned and pool workers pass no `deployment_config`, so a pool worker serves workflows of every build. This replaces the epic body's "worker deployment versioning" and its detail issue 9 | `08-workflow-engine.md:73` (D6), `contracts/temporal/naming.yaml:15` and `contracts/temporal/README.md:21` [read] | D3, D.2 |
| 5 | `run_task` is one activity with a single attempt and cancellation type `WAIT_CANCELLATION_COMPLETED`, and the workflow owns the retry loop | `contracts/temporal/naming.yaml:21-24` [read], `08-workflow-engine.md:87` (D20) | A.1 |
| 6 | Pool workers hold no database access: no login, no DSN and no Secret of the app database reach `wdf-workers` | `06-artifact-passing.md:227` and `08-workflow-engine.md:79` (D12) [read] | D11, D.2 |
| 7 | The worker writes no compile cache entry and does no cache lookup. The workflow looks up and records the cache (`08-workflow-engine.md:41`, decision 10). This spec drops the worker's re-check of the cache key at task start, which the epic body asks for and the workflow spec keeps (`08-workflow-engine.md:319`), because it needs the database that decision 6 withholds, and the workflow's own lookup and its refusal of a `build-info.json` with another `cache_key` already cover it. That workflow spec line needs a Closure addendum, which H.1, the `revisit` child of #6, writes | `08-workflow-engine.md:41` [read], departing from `08-workflow-engine.md:319` and the epic body's "re-check the cache key at task start" [read] | D19, H.1 |
| 8 | This epic builds every pool image, and the toolchain epic ships packages and the hash-checked `wasmtime` and `wasm-tools` release assets, with no Dockerfile | `07-workload-toolchain.md:47` (decision 18) [read] | D16, D.1 |
| 9 | The first run of a component on a pod is a separate warm-up process (`wdf_runtime.command.warm_up`) and the guest then runs warm in a new process, so `GUEST_OVERHEAD_MIB` is the warm 128 MiB | `07-workload-toolchain.md:50` (decision 21), `packages/runtime/src/wdf_runtime/pins.py:9-11` [read] | D5, Q1, T.1 |
| 10 | A compile is two steps: `python -m wdf_compile.limits fetch` reaches the mirror and runs no author code, then `python -m wdf_compile build --find-links DIR` builds offline | `07-workload-toolchain.md:51` (decision 22), `packages/compile/src/wdf_compile/limits.py:1-6` [read] | Q2, T.2 |
| 11 | The task input carries the bound params, which the worker writes as `input.json`, and the worker reads from the task request event only `task_type`, `inputs`, `resume_checkpoint` and `deadline` and the attributes the stage table lists | `08-workflow-engine.md:43` (decision 12), `contracts/stages/stages.yaml:73-80` [read] | D6, 0.1 (issue) |
| 12 | The worker heartbeats from its own loop while a synchronous store transfer runs in a thread with the client's `progress` callback, because the artifact client never heartbeats itself | `06-artifact-passing.md:108,227` [read] | D4, A.1 |
| 13 | `paced_sim`, the cooperative stop marker, the checkpoint format and kill-and-resume belong to the checkpoint epic (#18). This spec also moves there the worker's final checkpoint and `operator_interrupt`, which decision 7 of the workflow spec still gives the worker (`08-workflow-engine.md:38`), and #18 confirms the move in its own spec (the #18 row of the rules below) | `07-workload-toolchain.md:42` (decision 13), `08-workflow-engine.md:46` (decision 15) [read] | D9 |

### 0.2 Decided questions

The captain chose option (a) of each question on 2026-11-15, and the lanes build on it.
No decision changes a pinned contract, except the fail path of Q1, which states its one contract change and its version bump.

| # | Question | Options | Decision |
| --- | --- | --- | --- |
| Q1 | The worker process shares the simulate pod with `wasmtime`. S6 measured a first run at a working set of 290 to 326 MiB in the 384 MiB slot with no worker beside it, and at worst 58 MiB to spare (`docs/delivery/spikes/S6/cold-start.md:15-17,97`). A Python process with `temporalio` and `boto3` imported is estimated at 100 to 150 MiB (estimate, unmeasured), so the warm-up of decision 9 is not expected to fit. What happens when spike S11 confirms it does not? | (a) S11 measures the worker beside the warm-up and a warm task on the real workload component. On a fail, a `revisit` child of #2 grows the `ci` simulate node and slot by the smallest 128 MiB step that S11 shows passing, and changes the simulate row of `contracts/temporal/pools.yaml` in the same PR, because `test_pools_match_ci_slots` holds the two equal (`packages/workflow/tests/test_pools.py:33-38`) and fails on either half alone, so D.2 waits for that PR and not for #9. That PR is labelled `contract-change` with its consumer list and bumps `contracts/temporal/VERSION` from 1 to 2 (`docs/delivery/method.md:101`), and it also edits `deploy/presets/`, a departure from `method.md:60` that section 3 declares. (b) Keep 384 MiB and move the first-run compile off the pod: a precompiled `.cwasm` built on the compile pool, which is a new artifact kind and a contract change that the toolchain epic rejected (`07-workload-toolchain.md:71`, D7). (c) Keep 384 MiB with a second container in the pod that runs only `wasmtime`, fed by a shared scratch volume and a small exec agent, which splits the slot between two limits and adds a process protocol | (a), decided by the captain on 2026-11-15. It measures before it resizes, the compile and aggregate slots of 1 GiB already hold S1b's 417 MiB build peak and the aggregate's third of a slot with room for the worker (estimate, S11 records both), and the only contract file it can touch is the one #9 replaces anyway, by one row that #9 then carries |
| Q2 | Decision 10 runs the `componentize-py` step with the container network off, which a long-lived pod cannot do for one step, because the worker needs Temporal and the store for the whole task, and S1b found `unshare --user --net` refused under the default seccomp profile (`07-workload-toolchain.md:51`). How is the build step held off the network in a pod? | (a) A NetworkPolicy on each pool pod allows egress only to DNS, the Temporal frontend and the object store, plus the mirror on the compile pod, and no ingress, so the build step can reach only those, while fetch and build stay the two steps of decision 10. Author code at build time already runs inside the wasm sandbox (`07-workload-toolchain.md:73`, D9). kindnet enforces NetworkPolicy through kube-network-policies [looked up: kindnet.sigs.k8s.io, "Network Policies"], which D.2 proves on the cluster. (b) The compile pod runs with seccomp `Unconfined` and the build step under `unshare --user --net`, keeping no network at all but dropping the default seccomp profile for the whole pod. (c) A pod per compile, which contradicts the long-lived slot workers of the epic | (a), decided by the captain on 2026-11-15. It keeps the default seccomp profile, reaches the same three platform endpoints the worker needs anyway, and adds one manifest per pool. If D.2 finds the policy not enforced, the fallback is (b) for the compile pod only, which D.2 raises as needs-decision and a follow-up T.2 issue applies to `pools/compile.py`, with the pod's seccomp field in D.2's manifests |
| Q3 | Since Kubernetes 1.28 the kubelet sets `memory.oom.group` on cgroup v2, so an OOM kill takes every process of the container, the worker with it, and the kubelet setting `singleProcessOOMKill` (1.32 and later, the kind node is v1.35.0, `deploy/versions.yaml:4-7`) restores a per-process kill [looked up: kubernetes.io blog "The Shift to cgroup v2 in Kubernetes", kubernetes/kubernetes PR 126096]. With the default, an OOM reaches the workflow as `heartbeat_timeout`, so `oom_killed`, `compile_oom` and `aggregate_oom` are never reported and the workflow's two-in-a-row rule (`08-workflow-engine.md:70`, D3) never fires. Who sets the kubelet, and when? | (a) Defer: this epic changes no kubelet setting, the runner reports an OOM class when the child ends with a non-zero exit or a signal and the container's own `memory.events` `oom_kill` count rose (D8), which fires only on a per-process kill (T1 under Docker, or the kubelet setting once #15 turns it on), because under the default group kill the worker dies with the child, and the failure scenarios epic (#15), which provokes OOM on purpose, decides `singleProcessOOMKill` with a `revisit` child of #2. (b) This epic files a `revisit` child of #2 that adds `singleProcessOOMKill: true` to the kubelet patch of the three pool nodes (`scripts/cluster/render.sh:75-87`) and proves `oom_killed` on the cluster | (a), decided by the captain on 2026-11-15. OOM is not on the proof-of-concept path: no step of the #17 core pass provokes one, and until #15 an OOM still costs one infrastructure attempt, which is the budget `oom_killed` charges on the simulate pool (`contracts/activity/failure-classes.yaml:8-14`) |
| Q4 | `JobWorkflow` has never run against a real pool: the harness always starts a `FakePool` per pool (`packages/workflow/src/wdf_workflow/harness/cluster.py:272-281`, no flag in `harness/__main__.py:30-51`), its smoke bundle holds a module with one constant and no `Run` export (`harness/__init__.py:119-130`), and its simulate params name a workload `pi` that the workload library does not have (`harness/__init__.py:160`, `packages/workloads/src/wdf_workloads/main.py:19`). How does this epic drive a real job through the deployed workers? | (a) A `revisit` child of #6 adds `--pools real` to the harness: it starts no `FakePool`, seeds the workload library bundle (`python -m wdf_workloads.bundle`) with entry `wdf_workloads.main`, level 0 `echo` params from `contracts/workload/vectors/level0/echo/input.json` and aggregation `digest_list`, checks every result against the level 0 expected vector, and accepts the worker Deployments as `restart --target` names. (b) This epic's own driver, `python -m wdf_worker.drive`, imports the harness seeding and starts `JobWorkflow` itself, with no revisit but a second copy of the start path. (c) No `JobWorkflow` run in this epic: `run_task_once` per task only, and the first real job is the smoke of #17 | (a), decided by the captain on 2026-11-15. The harness already seeds rows and manifests as #24 would, claims rows as #8 would and opens its own port-forwards, so a flag is the smallest change, and #17 and #15 reuse it. The same revisit adds the queue prefix of D22, so a fake run never shares a queue with the deployed workers |

### 0.3 Defaults the lanes build on

Numbers marked as estimates are fixed by S11, a lane's measurement or #16.

| # | Topic | Default | Reason, and the alternative | Changes |
| --- | --- | --- | --- | --- |
| D1 | Package | One package `packages/worker/` (`wdf_worker`, distribution `wdf-worker`) with one extra per pool: `compile` adds `wdf-compile` and `uv` (D16), `simulate` adds `wdf-runtime`, `aggregate` adds `wdf-aggregate`. The core depends on `wdf-workflow` for `TaskInput`, `TaskResult`, `FailureDetails`, `HeartbeatDetails` and the naming contract, on `wdf-artifactclient`, `wdf-keys`, `wdf-events`, `wdf-stages` and `wdf-temporal`. `wdf-workflow` brings `wdf-compile`, `wdf-runtime`, `wdf-itest` (with `pytest`) and `componentize-py` into the core (`packages/workflow/pyproject.toml:5-26`; `uv export --frozen --package wdf-workflow` lists them), so every pool image carries that chain, as D11 accepts `psycopg`. What sets a pool image apart is its binaries and its entry point: `uv` and `wasm-tools` on compile, `wasmtime` on simulate, `wdf-aggregate` on aggregate, and `python -m wdf_worker --pool <pool>` as each image's command. Tests reach `wdf-itest`, `wdf-workloads` and `wdf-compile` through the root dev group, which already lists every workspace package | One codebase as the epic says. The alternative, three packages, repeats the activity and the service three times. Moving the models out of `wdf-workflow`, so that an image holds only its own pool's library, was rejected: it changes a closed epic's package for image size only (about 46 MiB of `componentize_py` in the project's `.venv`), with no runtime gain, as D11 says | 0.1 (issue), D.1 |
| D2 | Slots and replicas | One task per pod (`deploy/presets/slots/ci.yaml:1`), so each pool is one Deployment with `replicas: 1`, `strategy: Recreate` and `max_concurrent_activities=1`, and the pod's requests equal its limits equal the slot | The epic body's "several slots per pod" on simulate contradicts every slots preset. A surge pod does not fit a pool node, which holds one slot (`scripts/cluster/slots.sh ci`: compile and aggregate keep 300m and 110 MiB beside the slot, simulate 50m and 30 MiB) | A.2, D.2 |
| D3 | Versioning | No `deployment_config`, no build id in routing, and the image tag carries the git short sha for tracing only | Decision 4. S9 showed an unversioned activity worker serving both workflow builds (`08-workflow-engine.md:73`). The body's request that #2 reserve room for two pool builds is withdrawn | D.2 |
| D4 | Heartbeats | The activity heartbeats from its own asyncio task every `min(10 s, heartbeat timeout / 3)` (estimates), whatever the child prints, with `HeartbeatDetails` holding the latest parsed guest or aggregate `HEARTBEAT` line or three nulls during staging, warm-up, compile and upload, `checkpoint` null in this pass, and `started_at`. A store transfer runs in a thread through `asyncio.to_thread`, and its `progress` callback only records the bytes moved | The contract runs `run_task` with a 5 s heartbeat timeout (`packages/workflow/src/wdf_workflow/testing/pool.py:339-340`), the pools give 60 s (`contracts/temporal/pools.yaml:8-25`), and a cancel reaches an activity only on a heartbeat reply. The warm-up of about 29 s at 250m prints nothing (S6) | A.1 |
| D5 | Child processes | Every child (the warm-up, `wasmtime`, `wdf_compile.limits fetch`, `wdf_compile build`, `wdf_aggregate run`) starts through one runner with `asyncio.create_subprocess_exec`, `start_new_session=True` and a `preexec_fn` that sets `PR_SET_PDEATHSIG` to `SIGKILL` through `prctl`, stdin from `/dev/null`, stdout and stderr merged and read in chunks of 65,536 bytes, split into lines by the runner, into a tail of 65,536 bytes (`contracts/artifact/staging.yaml:16-19`) and the heartbeat parser `wdf_runtime.heartbeat.beats`. `StreamReader.readline` is not used, because it raises once a line passes the stream limit, where `run.py` bounds each read (`packages/runtime/src/wdf_runtime/run.py:103-111`). A stop sends `SIGTERM` to the process group, waits 5 s (estimate) and sends `SIGKILL` to the group | `wdf_runtime.run.run` and `warm` use a blocking `Popen` and `subprocess.run` with no process group, live output or cancel (`packages/runtime/src/wdf_runtime/run.py:44-47,84-95`), and `wdf_compile` starts `uv` and `componentize-py` as grandchildren (`packages/compile/src/wdf_compile/build.py:161-173`), which the group kill reaches. The runner reuses `wdf_runtime.command`, `warm_up`, `classify`, `read_error_file`, `copy_input`, `input_digest` and `warmed`, and records a warm-up as `run.py:92-95` does, so `wdf_runtime` is not changed | R.1 |
| D6 | Deadlines | The worker's deadline is the activity's `started_time` plus its `start_to_close_timeout` from `activity.info()`, less an upload reserve of 30 s (estimate). The request's `deadline` is logged only, because the workflow computes it at schedule time (`packages/workflow/src/wdf_workflow/taskpath.py:246,252`) while Temporal starts the start-to-close clock at activity start (`taskpath.py:274-275`), so a task that waited in the queue would start with its time already spent, and the activity vectors carry a fixed past `deadline`. The simulate guest gets `-W timeout` of what is left after staging and the warm-up, the compile fetch and build each their `--timeout` of what is left (a fetch past it is `compile_timeout` through `limits.classify`, `packages/compile/src/wdf_compile/limits.py:84-96,106`), and the aggregate, which takes no timeout, is stopped by the runner at the deadline and reported `aggregate_timeout` | The request carries no run timeout, `cpu_cores` and `memory_mib` are outside the worker's stage reads (decision 11), and queueing on a single-slot pool must not spend the guest's budget. A changing `-W timeout` value does not change the module cache entry (`packages/runtime/tests/test_pin_wasmtime.py:262-285`) | T.1, T.2, T.3 |
| D7 | Failure classes | Simulate: `wdf_runtime.classify` with the runner's facts. Compile: `wdf_compile.limits.classify` for exits 3, 4 and 5, and exit 1, which has no compile class in version 1 (`contracts/workload/README.md:65`), as `worker_shutdown` with the reason `worker_shutdown: compile-library-error: <detail>`, because an unexpected library error is more likely a platform fault (an unwritable cache, a missing tool) than the author's, and `worker_shutdown` is retried on the infrastructure budget. Aggregate: its native codes (`contracts/workload/exit-classes.yaml:53-82`). Staging and collection: the `code` of the `ArtifactError` raised. Any other exception of the worker itself: `worker_shutdown` with the reason `worker_shutdown: worker-error: <type>: <detail>`. Every failure is an `ApplicationError` whose `type` is the class, `non_retryable` as the row says, with `FailureDetails` holding the uploaded log tail. Every reason is cut to `REASON_MAX` (1,024) characters by shortening its `<detail>` (`packages/workflow/src/wdf_workflow/models.py:49,121`) | Every reported type must be a row of `contracts/activity/failure-classes.yaml`, and the workflow turns an unknown type into an attempt it cannot budget. A traceback or exception text has no length cap, and a reason past `REASON_MAX` fails the `FailureDetails` model, which would end a retryable `worker_shutdown` as a failed task. The alternatives for compile exit 1 are `compile_failed`, which is deterministic with no retry (`contracts/activity/failure-classes.yaml:15`) and blames the author for a platform fault, or a new class, which is a contract change | T.1, T.2, T.3, A.1 |
| D8 | OOM | A child that ends with any non-zero exit or a signal while the container's `memory.events` `oom_kill` count is above the value read before it started is the pool's OOM class: `oom_killed` through `Facts(oom_event=True)` (`packages/runtime/src/wdf_runtime/classify.py:61-64`), `compile_oom` or `aggregate_oom`. The count is compared on every non-zero exit, because the kernel may kill a grandchild and leave the child to exit by itself: `wdf_compile build` runs `componentize-py` as a subprocess and reports any non-zero return as `compile_failed` (`packages/compile/src/wdf_compile/build.py:138-142,161-172`). A `SIGKILL` with no new OOM count is `host_error` on simulate and `worker_shutdown` on the other pools. These rules cover only kills the runner did not send: a stop the runner sends keeps the class of its cause, the deadline of D6, the drain of D10 or the cancel of D9. No Kubernetes event is read and no service account token is mounted. Under the default group kill of Q3 the worker dies with its child, so on the `ci` cluster this rule never fires and the attempt reaches the workflow as `heartbeat_timeout` | The container's own cgroup counter replaces the "node events" of the toolchain rules row (`07-workload-toolchain.md:110`) with no API access, and it is all the worker can see under Q3 (a). It stays in R.1, although no cluster run of this epic reaches it, because T1 under Docker and the per-process kill that #15 may turn on do reach it, and it costs one counter read per child. The alternative, dropping the fact and leaving all OOM classification to #15, would report a grandchild OOM as `compile_failed`, a deterministic author fault with no retry | R.1, T.1, T.2, T.3, Q3 |
| D9 | Cancel, pause and checkpoints | Every cancel stops the child group and re-raises the cancellation, which the workflow reads as a withdrawn attempt and returns the task to `ready` (`packages/workflow/src/wdf_workflow/taskpath.py:283-288`). No checkpoint is written, `resume_checkpoint` is read and not mounted, so a resumed task runs from its start, and `operator_interrupt` is never raised. `activity_contract` runs with `interrupts=None`, which `ContractCases` allows for a pool without one (`testing/pool.py:389-396`) | Decision 13. A manual pause cancels only running tasks whose plan sets `checkpoint` (`packages/workflow/src/wdf_workflow/ready.py:68-73`), a flag any template can set on a simulate task today (`packages/jobdoc/src/wdf_jobdoc/models.py:291`). Such a task is withdrawn and returns to `ready` with no checkpoint, and every other running task runs to completion. #18 adds the final checkpoint, `operator_interrupt` and the interrupts case | A.1, section 2 |
| D10 | Shutdown | On `SIGTERM` the worker stops polling, lets a running task go on for 30 s (estimate, `graceful_shutdown_timeout`), then stops the child group and fails the attempt as `worker_shutdown` with its log tail. The SDK delivers that drain as a cancellation, so the cancel handler of D9 checks `activity.cancellation_details().worker_shutdown` first and raises the `worker_shutdown` `ApplicationError` instead of re-raising, which the SDK would otherwise report as a cancel and the workflow as a withdrawn attempt. A server cancel that lands during the drain has `worker_shutdown` false and is withdrawn as D9 says. No separate final heartbeat is flushed: the failure details carry the log tail, and in this pass a beat would carry no checkpoint (D9), so #18 adds the final beat with the final checkpoint. `terminationGracePeriodSeconds` is 75 (estimate: the drain, the 5 s kill and an upload) | A compile may run 600 s (`compile_run_timeout_s`), longer than any grace worth waiting, and `worker_shutdown` charges the infrastructure budget. `activity.is_worker_shutdown()` is not used, because it is true from the start of the drain (`temporalio` 1.35.0, `worker/_worker.py:907`), while the cancellation details record the cause of each cancel (`activity.py:181,322`, `worker/_activity.py:230-236`) | A.2, D.2 |
| D11 | No database | The pool images carry `wdf-workflow` for its models and with it the `psycopg` wheel and the chain of D1, but no DSN, login or database Secret reaches `wdf-workers`, and a render test refuses any `WDF_*_DSN` variable | Decision 6. Moving the models out of `wdf-workflow` is a change to a closed epic for no runtime gain | D.2 |
| D12 | Store identity | Each pool reads one Secret `<pool>-worker-store` in `wdf-workers` holding only its pool's identity (`contracts/store/roles.yaml:15-25`), rendered from `scripts/storage/store/identities.sh path` as `deploy/workflow/logins.py:66-81` renders the workflow worker's. The aggregate child gets the same file through `--credentials` with `--role aggregate` | Least privilege per pool, and the pattern exists | D.2 |
| D13 | Scratch | One disk `emptyDir` per pod mounted at `/var/wdf` for task scratch, the `wasmtime` module cache, `HOME`, `TMPDIR`, `UV_CACHE_DIR` and `XDG_CACHE_HOME` (each a directory under it) and the readiness file, never `medium: Memory`, which counts against the pod limit. Task scratch and `TMPDIR` are emptied when a task starts, and task scratch again after it ends, because a child stopped by a cancel, a drain or a group OOM leaves its `tempfile` tree under `TMPDIR`, and a group OOM restarts the container but keeps the `emptyDir`. The module cache is not pruned in this pass, at about 10.6 MiB per component (S6), and the volume has a `sizeLimit` of 1 GiB (estimate) | The root filesystem is read-only and `uv`, `componentize-py` and `tempfile` write to `HOME` and `TMPDIR` (`packages/compile/src/wdf_compile/build.py:62`), so a compile's site-packages install needs the large volume, not a 64 MiB `/tmp` like the workflow worker's (`deploy/workflow/Tiltfile.module:96,101`). The cache survives a container restart inside the pod, and a pruning rule waits for #16's soak | A.1, D.1, D.2 |
| D14 | Probes, logs and metrics | Readiness is the file `/var/wdf/ready`, touched once the worker polls, as the workflow worker touches its own. Logs are plain text on stdout. No metrics port and no SDK Prometheus endpoint in this pass | Metrics and JSON logs belong to #13, which defines slot usage, heartbeat lag, exit classes, OOM count and build per pod. A metrics port now would ship an unnamed catalogue | A.2, D.2, section 2 |
| D15 | Profiles and dependencies | The three worker modules are in profile `ci` only and depend on `infra/temporal-bootstrap` and `infra/objectstore-bootstrap`, and the compile module also on `infra/mirror`, which exists in `ci` only (`deploy/mirror/module.yaml:18`). `dummy-workers` stays | Tilt fails a profile whose module depends on a disabled one (`Tiltfile:49-51`), so `dev` and `full` keep no worker until the mirror question of those presets is answered by their owner. The dummy fits beside the compile slot (`slots.sh ci`) | D.2 |
| D16 | Images | One `deploy/workers/Dockerfile` with a build argument per pool and one `deploy/workers/image.sh <pool>`, from the `images.python` pin (`deploy/versions.yaml:34-37`), with the wheels of `uv export --package wdf-worker --extra <pool>`, which include the `wdf-workflow` chain of D1 on every pool, and the command `python -m wdf_worker --pool <pool>`. `image.sh` downloads the upstream `wasm-tools` (compile image) and `wasmtime` (simulate image) release assets from the `url` of `nix/wasm-tools.nix` and refuses an asset whose SHA-256 differs from its `hash`, because the dev shell's binaries are patched to the Nix store's loader (`nix/wasm-tools.nix:16`) and do not run in the base image. The compile extra of D1 lists `uv` at the dev shell's version (0.11.21 today) as a Python dependency, so `uv.lock` holds its wheel hashes and the wheel's static binary lands on the image's `PATH`. The aggregate image gets no WASM tool. The image fixes each binary's modification time, which names the `wasmtime` module cache (`docs/delivery/spikes/S6/cold-start.md:138-139`), and the cache lives in the pod, so no equality with the host is needed. Each image runs as user 65532, and the pod settings of D.2 add a read-only root filesystem and no service account token | Decision 8, and `docs/versions-policy.md:15` says the compile image uses the dev shell's tool versions. No new pin enters `deploy/versions.yaml`: the two release assets reuse the Nix pins and `uv` rides `uv.lock`. The alternative, copying the Nix closure into the image, ships hundreds of MiB of store paths | D.1, D.2 |
| D17 | Cluster budget | The three pods use the pool nodes only: compile 1,000m and 1,024 MiB, simulate 250m and 512 MiB, aggregate 1,000m and 1,024 MiB, which with `dummy-workers` is 2,260m and 2,568 MiB of the `wdf-workers` quota of 3,360m and 3,100 MiB (`scripts/cluster/namespaces.sh render ci`). Nothing is added to the infra node or to `wdf-control`, so the reserve rows of the capacity revisit (RabbitMQ 256 MiB, dispatch 256 MiB, admission 128 MiB on the 6,144 MiB infra node) are untouched | The capacity revisit has landed (`c8fa830`, `deploy/presets/ci.yaml:8,18`) and left the pool nodes and slots as they were. Under Q1 (a) a fail grows only the simulate node, which the S11 revisit did, to 896 MiB (https://github.com/AndoNorth/wasm-dag-forge/issues/380) | D.2 |
| D18 | Real pool values for #9 | Closure lists per pool, as the input #9 turns into `pools.yaml`: the worker's idle and peak resident memory and the warm-up and staging seconds from S11, the cancel latency that A.1's `cancel_stops_child_and_withdraws` prints and the drain time that A.2's `sigterm_fails_long_task` prints, the heartbeat interval of D4 as configured, and the pod's peak working set (`memory.current` less `inactive_file`) and peak anon from S11, with exit row 10's `anon` as a check | Decision 2. This epic does not edit `contracts/temporal/pools.yaml`, which #9 owns | S11, section 7 |
| D19 | Compile cache | The worker does no cache I/O. It returns the `build-info.json` that `wdf_compile build` writes with its `cache_key`, and the workflow's `cache_record` refuses an entry whose key differs from its own (`08-workflow-engine.md:41`) | Decision 7 | T.2 |
| D20 | Client and stage | The worker's client is built as the workflow service builds its own, with `event_data_converter` and `tracing_interceptor()` (`packages/workflow/src/wdf_workflow/service/__init__.py:159-165`), because `activity_contract` asserts the converter (`testing/pool.py:469-471`). The worker passes `run_stage_contract("worker", ...)` (`packages/stages/src/wdf_stages/contract.py:85`) | One wire format with the workflow, and the stage table is the source of what the worker reads | 0.1 (issue), A.1 |
| D21 | CI | No new CI workflow and no change to `.github/workflows/`. The `state-modules` pull request filter does not list `deploy/workers/**`, so D.2 runs `scripts/tilt/run.sh ci` itself and pastes the output, and the exit test records a green run on `main` | The workflow engine epic recorded the same choice (`08-workflow-engine.md:92`, D25) | D.2, section 7 |
| D22 | Pool queue selection | Fake and real pools serve the one `run_task` interface on the same queue names. The pool queue of a run is its prefix followed by the name in `naming.yaml` (`wdf-simulate` with no prefix, `h-1a2b3c4d-wdf-simulate` with the prefix `h-1a2b3c4d-`), and the prefix is empty by default and keeps each name within the `QUEUE` pattern (`packages/workflow/src/wdf_workflow/models.py:51`). `FakePool` already takes the full name through `queue` (`packages/workflow/src/wdf_workflow/testing/pool.py:129,134`) and `activity_contract` the queue it checks (`testing/pool.py:460`), so neither changes. The worker service reads an optional `WDF_POOL_QUEUE_PREFIX` (0.4), which the deployed modules never set, so the deployed workers keep the contract names. `JobWorkflow` reads a job's prefix from the memo key `wdf_pool_queue_prefix` of its start, a constant of `wdf_workflow.taskpath`, empty when absent, and applies it to the queue of each placement before the `task.placed` event (`taskpath.py:231-239`), so the placer and `pools.yaml` keep the contract names and a job with no memo runs exactly as before. The harness gives each `--pools fake` run a fresh prefix and `--pools real` none, and `--queue-prefix` names one for either (H.1). A run pairs fake and real workers per pool by starting each on the same prefix | The deployed workflow worker places every job on the contract names (`placement.py:81,101`) and `FakePool` defaults to them, so after D.2 a fake run on the cluster shares each queue with a real worker, which takes a harness compile and fails it as `compile_failed` (the smoke bundle has no `Run` export, `harness/__init__.py:119-130`). A prefix per job lets one run hold its own queues beside the deployed workers, and the smoke, #15's scenarios and a later load framework reuse it. Rejected: scaling the worker Deployments to 0 for a fake run, which stops real runs for its length and cannot pair fake and real workers; a queue map on the placer, which the one deployed workflow worker shares across every run; and a field of `JobParams`, a model of the closed envelope epic that admission fills from the row (`packages/events/src/wdf_events/models.py:229-239`) | H.1, A.2, D.2, section 7 |

### 0.4 Proposals the lanes pin

| Item | Proposal | Epic item |
| --- | --- | --- |
| Activity | `wdf_worker.activity.run_task(task: TaskInput) -> TaskResult`, registered under the `run_task` name of `naming.yaml`, which dispatches on `task.request.data.task_type` to `wdf_worker.pools.<pool>.run(task, context)`, where the context holds the store client, the scratch root, the slot memory and the heartbeat state | detail issues 1, 4 |
| Runner | `wdf_worker.runner.Child`: `start(argv, cwd)`, an async iterator of output lines, `stop()` for the group stop of D5, `wait() -> Exit(code, signal, oom)` with the OOM fact of D8, and `tail` | detail issues 3, 5 |
| Service | `python -m wdf_worker --pool {compile,simulate,aggregate}` runs one activity worker on the pool's queue from `naming.yaml` behind the prefix of D22, with `max_concurrent_activities=1`, no deployment config and the drain of D10. Configuration comes from the environment only: `WDF_TEMPORAL_ADDRESS`, `WDF_NAMESPACE`, `WDF_STORE_ENDPOINT`, `WDF_STORE_CREDENTIALS`, `WDF_SLOT_MEMORY_MIB` (the downward API `limits.memory` in MiB), `WDF_SCRATCH`, `WDF_READY_FILE`, `WDF_MIRROR` on compile, and the optional `WDF_POOL_QUEUE_PREFIX`. A missing required variable or a prefix outside the `QUEUE` pattern exits 2, as the workflow service does | detail issues 1, 8 |
| Guest memory | The simulate pool passes `memory_mib = WDF_SLOT_MEMORY_MIB - WORKER_RESERVE_MIB` to `wdf_runtime.command`, with `WORKER_RESERVE_MIB` a constant of `wdf_worker` that S11 sets (128, estimate), so the guest cap is the slot less the worker and the 128 MiB of `GUEST_OVERHEAD_MIB` | detail issue 3, Q1 |
| Contract cases | `wdf_worker.testing.contract_cases(pool, store) -> ContractCases` seeds what each case needs (the workload library bundle in `wdf-sources`, its component in `wdf-components`, a level 0 result and an index put with `wdf_artifactclient.aggregate.put_index`) and builds a succeeding and a failing `TaskInput` per pool from `contracts/activity/vectors/task-input/valid/` | `activity_contract` for #10 |
| Egress probe | `python -m wdf_worker.probe` prints one line per target, `<name>: ok` or `<name>: refused`, for the Temporal frontend, the store, the mirror and the app database, whose in-cluster Service names and ports are constants of the probe and not configuration, and exits 0 when each answer is what the pool's NetworkPolicy allows | Q2 |
| Modules | `deploy/workers/{compile,simulate,aggregate}/` each with `module.yaml` (group `workers`, namespace `wdf-workers`, profile `ci`, pin `python`), `Tiltfile.module` (Deployment `<pool>-worker`, the pool's node selector and toleration from `contracts/topology/topology.yaml`, Secret, NetworkPolicy, `emptyDir`), and readiness `kubectl wait --for=condition=Available deployment/<pool>-worker` | detail issue 2 |

### Rules agreed with epics that do not exist yet

These are stated so the owner epic confirms them in its own spec.

| Epic | Rule |
| --- | --- |
| #9 Placement | Turns the figures of D18 into `contracts/temporal/pools.yaml` by a contract-change PR, keeps the three queue names, and keeps the simulate slot of Q1 (a) when S11 failed |
| #18 Checkpoint | Adds the checkpoint writer, the `/ckpt` and `/resume` mounts, the final checkpoint and `operator_interrupt` on cancel, the checkpoint reference in heartbeat and failure details, and runs `activity_contract` with an interrupts case on `wdf-simulate`, inside `packages/worker/src/wdf_worker/pools/simulate.py` through its own lanes |
| #13 Observability | Defines and adds to `metrics.yaml` the worker metrics (slot usage, heartbeat lag, exit classes, OOM count, image per pod), turns on the SDK metrics port and JSON logs in `wdf_worker.service` |
| #15 Failure scenarios | Provokes OOM on each pool, decides `singleProcessOOMKill` with a `revisit` child of #2 (Q3), and drives worker crash, `SIGTERM` during a task, node loss, rollout and an engine outage longer than the heartbeat timeout through the harness of Q4 |
| #16 Soak | Measures the module cache growth of D13 and sets its pruning rule, and replaces the estimates of D4, D6 and D10 |
| #17 Test ladder | The smoke runs its level 0 job on these workers, and the e2e core pass runs both compile chains on the one compile pod one after the other (decision 3) |

## 1. Goal and non-goals

Goal: Kubernetes workers run the three task types (compile, simulate, aggregate) and report back through Temporal.
Each pool is one long-lived single-slot pod that polls its own unversioned queue, serves `run_task` by staging inputs, running one child at a time in its own process group, heartbeating from staging to upload, classifying the end, and returning a `TaskResult` or a typed failure.
A worker can be killed, drained or rolled without losing a job, because the workflow owns every retry.
Only simulate tasks run a WASM guest: compile and aggregate run platform-defined native code.

### Breakdown inputs updated for what landed

The epic body is not edited.
The rows below that depart from it, among them the versioning, the slots, the OOM detection and the checkpoint writer with `operator_interrupt`, are confirmed by the captain's approval of this spec, which is the alignment step (`docs/delivery/method.md:11-12`).
At filing, the filer comments them on #10 with a link to this table, so the epic issue points at what is built.

| Epic body item | Now | Source |
| --- | --- | --- |
| Contract versions | events v3, artifact v3, store v3, workload v1, activity v1, temporal v1, stages v1, module v1, topology v1 | the `VERSION` files [read] |
| "Several slots per pod" on simulate, `max_concurrent_activities` as the slot count | One task per pod on every pool | `deploy/presets/slots/ci.yaml:1` [read], D2 |
| "Pool workers use the same worker deployment versioning", detail issue 9, the build id per pod in the pool table and the #2 headroom request for two pool builds | Every pool queue unversioned, no build id in routing, no second pool build | decision 4, D3 |
| "The compile worker writes the cache entry" and "re-check the cache key at task start" | No cache I/O in the worker | decision 7, D19 |
| "Honouring cancel, pause and `not_found` from the heartbeat reply" | A cancel arrives on a heartbeat reply and stops the child group. The server-side activity pause is not used (`08-workflow-engine.md:38`, decision 7), and a zombie's completion is refused by the server, which costs the worker nothing | D9 |
| Checkpoint writes (detail issue 6), shared with #18's "worker checkpoint wrapper" | Deferred to #18 | decision 13, D9 |
| "On `SIGTERM` stop polling, flush a final heartbeat, give running tasks a grace period" | No separate final beat in this pass, and #18 adds it with the final checkpoint | D10 |
| The cold start of about 1.4 s and 0.4 GiB | The warm-up of about 29 s at 250m with a 290 to 326 MiB working set, in its own process | decision 9, S6 |
| Informal failure class names | The rows of `contracts/activity/failure-classes.yaml`, including `host_error`, `guest_memory_cap`, `worker_shutdown` and `aggregate_input_refused` | D7 |
| "OOM is exit 137 plus node events" | The container's `memory.events` count, and the group OOM kill of cgroup v2 | D8, Q3 |
| The #9 Interfaces row: the pool table "defined by this epic's manifests and read by the scheduler" | `pools.yaml` is a contract that #9 owns, held equal to the slots preset, and this epic supplies measured figures | decision 2, D18 |
| #17's "at least two compile pods on different nodes" | Dropped | decision 3 |
| Worker metrics and probes (detail issue 10) | Readiness only, metrics to #13 | D14 |
| Worker failure scenarios (detail issue 11) | #15, through the harness of Q4 | section 2 |

### Delivered already, and what is left

| Epic body item | State on `main` | Where it is, or what is left |
| --- | --- | --- |
| Guest command line, warm-up command line, exit classification, heartbeat parser, pins | Delivered by #11 | `packages/runtime/` (`wdf_runtime`) |
| Build library with the offline fetch | Delivered by #11 | `packages/compile/` (`wdf_compile`, `wdf_compile.limits`) |
| Aggregate library with progress lines and exit codes | Delivered by #11 | `packages/aggregate/` (`wdf_aggregate`) |
| Staging, collection, log tail, typed errors | Delivered by #12 | `packages/artifactclient/` (`stage_inputs`, `collect_outputs`, `upload_tail`) |
| Activity payloads, failure class table, naming, pool table v0 | Delivered by #6 | `contracts/activity/`, `contracts/temporal/` |
| Conformance test and fake pool | Delivered by #6 | `wdf_workflow.testing.activity_contract`, `FakePool` |
| Store identities per pool, pool label and taint, `wdf-workers` quota, mirror | Delivered by #3, #2, #12 | `contracts/store/roles.yaml`, `contracts/topology/`, `scripts/cluster/namespaces.sh`, `deploy/mirror/` |
| The runner with process groups, live heartbeats, cancel and deadlines | Left | R.1 |
| The three task paths | Left | T.1, T.2, T.3 |
| The activity, the service and conformance on every pool | Left | A.1, A.2 |
| Images and pool modules | Left | D.1, D.2 |

Non-goals for this pass:

- everything the epic lists under "Does not own": placement and the pool table (#9), workflow code and retries (#6), admission (#8), artifact and cache semantics (#12), the toolchain, the workloads and the checkpoint format (#11, #18), node and pool sizing (#2), autoscaling, pod-per-task execution and inbound surfaces
- checkpoints, `operator_interrupt` and resume from a checkpoint (D9, #18)
- OOM classification under the group kill and the kubelet setting (Q3, #15)
- worker metrics, JSON logs and dashboards (D14, #13)
- the `dev`, `full` and `soak` profiles (D15)

## 2. Interfaces

### Consumes (each pinned to a version)

| From | What | Pinned at | Stub used until it exists |
| --- | --- | --- | --- |
| Dev environment | Dev shell with `uv`, Python, `wasmtime` 48.0.7, `wasm-tools` 1.262.0, `kubectl`, `kind`; recipes `just test`, `just itest`, `just ci`, `just up`, `just down`, `just nuke`; `wdf_itest` | `flake.lock`, `nix/wasm-tools.nix`, `uv.lock` | none, exists [read] |
| Cluster | Preset `ci` with one compile, simulate and aggregate node, slots, pool label and taint, `wdf-workers` quota, module contract, cluster lock | `deploy/presets/ci.yaml`, `deploy/presets/slots/ci.yaml`, topology v1, module v1 | none, exists |
| Envelope | Event models, `TaskRequestData`, key formulas, stage table and `run_stage_contract`, `wdf_temporal` converter and tracing | events v3, keys v1, stages v1 | none, exists |
| Artifacts and state | `ArtifactClient`, `stage_inputs`, `collect_outputs`, `upload_tail`, `put_index`, typed errors, staging contract, store roles `compile`, `simulate`, `aggregate` | artifact v3, store v3, artifactclient v1 | none, exists |
| Toolchain | `wdf_runtime` (`command`, `warm_up`, `classify`, `heartbeat`, pins), `wdf_compile` (`build`, `limits`), `wdf_aggregate`, `wdf_workloads.bundle`, exit classes, level 0 vectors | workload v1, runtime v1, compile v1, aggregate v1 | none, exists |
| Workflow engine | `TaskInput`, `TaskResult`, `FailureDetails`, `HeartbeatDetails`, failure class table, naming, `activity_contract`, `run_task_once`, `JobWorkflow`, the deployed workflow worker, the harness | activity v1, temporal v1, workflow v1 | none, exists |
| Workflow engine, revisit | The harness `--pools real` of Q4 (a) and the queue prefix of D22 | not filed, a child of #6 filed at approval (section 4) | `run_task_once` per task until it merges |
| Cluster, revisit | The simulate slot of Q1 (a), only when S11 fails | not filed, a child of #2 filed by S11 on a fail | the 384 MiB slot |

The capacity revisit of #2 for RabbitMQ, admission and dispatch has landed (`c8fa830`) and blocks no lane of this epic (D17).
The names below were checked free on `main` [read: no `packages/worker/`, no `deploy/workers/`, no `docs/delivery/spikes/S11/`, no `docs/architecture/*-pool-worker.drawio`].

### Produces (each pinned precisely)

| To | What | Where it lives (path) | Shape | Version |
| --- | --- | --- | --- | --- |
| #6, #8, #17, #15, #18 | Pool workers serving `run_task` on `wdf-compile`, `wdf-simulate` and `wdf-aggregate` | `packages/worker/` (`wdf_worker`) | the activity contract v1, passing `activity_contract` on each queue | worker v1 |
| the cluster, #17, #15 | Three images and three modules | `deploy/workers/` | modules `compile-worker`, `simulate-worker`, `aggregate-worker` (module v1), Deployments of the same names in `wdf-workers` | module v1 |
| #9 | Real pool figures | `docs/delivery/spikes/S11/worker-in-slot.md` and this spec's Closure | the per-pool table of D18 | none |
| #18, #13 | The places they extend | `wdf_worker.pools.simulate`, `wdf_worker.service` | the rules rows above | worker v1 |

### Assumptions about other epics

Each line is checked in the review against the sibling specs of #7, #8 and #24, which are drafted in parallel and had no open pull request when this spec was written.
The drafts of #8 and #24 were read in their working copies, and the last three lines answer what they assume of this epic.

- #8 starts `JobWorkflow` with the options of `naming.yaml`, so the task input of every pool activity comes from the workflow unchanged, and admission needs nothing from the workers.
- #24 stores source bundles in `wdf-sources` in the `wdf_compile.bundle` format, which the `compile` identity reads with its `get` (`contracts/store/roles.yaml:15-18`), and the `submit` role change of the realignment answer (D7) does not touch the pool roles.
- #7 carries intake only, and no broker client or queue reaches `wdf-workers`.
- The priority class stays `standard`, and the worker reads `priorityclass` only as the stage table lists it, for tracing.
- #9 replaces `pools.yaml` after this epic closes and keeps `heartbeat_s` at 30 s or more, so the beat of D4 stays every 10 s and three beats fit in one timeout.
- #17's smoke and e2e reach the workers only through the workflow, and need no port or Service of their own.
- #8 proves admission with `FakePool`, and nothing here waits for admission. After D.2 the deployed workers poll the contract queues on every cluster run, so #8's spec must name the queue selection of D22: its driver runs its `FakePool`s on a prefix and admission starts those jobs with the memo of D22, or the driver's jobs share each queue with a real worker.
- #24 lists "the worker side of an interrupt" under this epic. This epic owns the worker side of a cancel (stop the child group, withdraw the attempt), and the checkpoint written on an interrupt and `operator_interrupt` stay with #18 (D9). A pause withdraws a running simulate task only when its plan sets `checkpoint`, a flag a template can set today (D9), and that task returns to `ready` with no checkpoint until #18 lands, while any other running task runs to completion.
- #24 expects real pool workers deployed before its exit test, which the agreed order of this epic before #8 and #24 gives, and its jobs reach them only with no prefix, the default of D22, which #24's spec names.

## 3. Lanes

Package and module names are proposals.
Lane 0 owns every file it creates until the owning lane takes over the glob, except `wdf_worker.testing`, which stays lane 0's.

Departures from the method, stated so the review approves them knowingly:

- S11 runs beside lane 0, because it writes only under `docs/delivery/spikes/`, and it gates D.2's simulate pod and `WORKER_RESERVE_MIB`.
- Lane 0 carries no `just/*.just` module and adds no recipe: tests run through `just test`, `just itest` and `just ci`, and the worker is a `python -m` command, as the toolchain and workflow epics recorded.
- The harness revisit of Q4 is a child of #6 that runs in this epic's window and starts beside lane 0, with its own glob in `packages/workflow/`.
- 0.1 commits every dependency and extra of D1 to `uv.lock`, where `method.md:84` says a dependency lands in the PR that needs it, so that no later lane edits `packages/worker/pyproject.toml` or the lock.
- On a fail of S11 (1), the `revisit` child of #2 of Q1 (a) is one PR that edits `deploy/presets/` and `contracts/temporal/pools.yaml` together, where `method.md:60` limits a `contract-change` PR to `contracts/`, because `test_pools_match_ci_slots` fails on either half alone.

| Lane | Workers | Owns (exclusive) | Issues | Needs |
| --- | --- | --- | --- | --- |
| 0 Skeleton | 1 | `packages/worker/{pyproject.toml,README.md}`, `packages/worker/src/wdf_worker/{__init__,config}.py`, the stubs `packages/worker/src/wdf_worker/{runner,activity,service,__main__,probe}.py` and `packages/worker/src/wdf_worker/pools/*.py` until their lanes take them, `packages/worker/src/wdf_worker/testing.py`, `packages/worker/tests/{conftest,test_wheel,test_stubs,test_stage_contract,test_config,test_contract_cases}.py`, the two `wdf-worker` lines of the root `pyproject.toml`, the `uv.lock` workspace entry, `docs/architecture/NN-pool-worker.drawio` and its svg | 0.1 | nothing (barrier, merged alone) |
| S Spike | 1 | `docs/delivery/spikes/S11/**`, the S11 row of `docs/delivery/spikes/README.md`, and on a fail of (1) the dated Closure addendum line of `docs/delivery/epics/02-cluster.md` for the Q1 revisit | S11 | nothing, beside lane 0 |
| R Runner | 1 | `packages/worker/src/wdf_worker/runner.py`, `packages/worker/tests/runner/**` | R.1 | lane 0 |
| T Task paths | 2 (T.1 and T.2 at once, T.3 by either) | `packages/worker/src/wdf_worker/pools/**`, `packages/worker/tests/pools/**` | T.1, T.2, T.3 | R.1, and S11 for T.1 |
| A Activity and service | 1 | `packages/worker/src/wdf_worker/{activity,service,__main__,probe}.py`, `packages/worker/tests/{activity,service}/**` | A.1, A.2 | R.1 for A.1, T.1 to T.3 for A.2 |
| D Images and modules | 1 | `deploy/workers/**` (Dockerfile, `image.sh`, the three module directories, the Secret and policy renderers and their tests), the `deploy/workers` entry of `TREES` in `packages/migrate/tests/test_credentials.py` | D.1, D.2 | lane 0 for D.1, D.1, A.2, S11 and the harness revisit for D.2 |
| H Harness revisit (child of #6) | 1 | `packages/workflow/src/wdf_workflow/harness/**`, `packages/workflow/tests/harness/**`, `packages/workflow/src/wdf_workflow/taskpath.py`, `packages/workflow/tests/workflow/test_queue_prefix.py`, and the dated Closure addendum line of `docs/delivery/epics/08-workflow-engine.md` | H.1 | nothing, beside lane 0 |

Shared-file rules:

- `uv.lock` is the one hotspot, and 0.1 commits it with the workspace entry of `wdf-worker` and every dependency and extra of D1, so no later lane edits `packages/worker/pyproject.toml`.
  A lock conflict is never resolved by hand, and `uv lock` is rerun after rebase.
- The root `pyproject.toml` changes only in 0.1, by one `wdf-worker` line in `[tool.uv.sources]` and one in the dev dependency group.
- `packages/worker/tests/conftest.py` is lane 0's and holds the fixtures that several lanes share (the dev server and SeaweedFS through `wdf_itest`, the contract cases), and a lane keeps its own fixtures in a `conftest.py` inside its own test directory.
- A test that needs a container carries `integration`, and a test that needs the dev server and the store together also carries `pair`.
  No pytest session holds more than two containers.
- `packages/migrate/tests/test_credentials.py` changes only in D.2, by adding `deploy/workers` to `TREES`.
- `docs/delivery/spikes/README.md` gets one appended row from S11.
- `docs/architecture/11-placement-and-workers.drawio` is not edited: its pool boxes show slot worker pods per pool and name no build or version [read], and the worker detail lives in lane 0's new pool worker diagram, which the exit-test worker re-checks.
- `deploy/versions.yaml`, `contracts/**`, `packages/runtime/**`, `packages/compile/**`, `packages/aggregate/**`, `packages/artifactclient/**`, `scripts/**`, `.github/**`, `justfile`, `just/*.just` and every other `deploy/` directory are not touched.
- No tracked file holds a credential: store credentials come from the state directory, as the workflow module does.

## 4. Issues

Each block below becomes one lane issue from the [lane issue template](../../../.github/ISSUE_TEMPLATE/lane-issue.md), as a sub-issue of #10, except H.1, which is a `revisit` sub-issue of #6, and the blocks are replaced by `#NNN` links after filing.
At approval every block is filed, H.1 included, except T.1 and D.2, which S11 gates and which are filed once its result merges (`docs/delivery/method.md:59,105`).
H.1 starts at once, beside lane 0 and S11.
Done means, for every issue: `just ci` is green on the branch, the output of the named tests is pasted in the PR, and nothing outside Owns and the shared files changed.
An issue that names a T2 command runs it under the cluster lock and pastes its output.

### Mapping from the epic's detail issues

| Epic detail issue | Delivered by |
| --- | --- |
| 1 Worker images and activity worker skeleton | 0.1, A.2, D.1 |
| 2 Pool manifests | D.2 |
| 3 Task runner | R.1 |
| 4 Heartbeat loop and cancel handling | A.1 |
| 5 Exit classification and OOM detection | R.1 (the OOM fact), T.1 to T.3 (the classes), with Q3 |
| 6 Checkpoint writer and resume entrypoint | #18 (D9) |
| 7 Artifact fetch and attempt-scoped upload | T.1 to T.3 |
| 8 Graceful shutdown and rollout | A.2, D.2 |
| 9 Worker versioning | dropped (decision 4) |
| 10 Worker metrics and probes | A.2 (readiness), #13 (metrics) |
| 11 Worker failure scenarios | #15 |
| 12 Compile runner | T.2 |
| 13 Aggregate runner | T.3 |

### 0.1 Package skeleton, stubs and contract cases

Lane 0, tier T0, and T1 for the contract cases.
Needs: nothing.
Blocks: every other issue but S11 and H.1.

- Scope: create `packages/worker/` with the extras of D1, the configuration model of 0.4 that exits 2 on a missing variable, stub modules that raise `NotImplementedError("not implemented: owned by lane <letter>")`, `wdf_worker.testing.contract_cases`, the stage contract test, the wheel test, and the pool worker diagram of the `run_task` path (stage, warm-up, run in a process group, heartbeat, classify, collect, tail, result).
- Owns: lane 0's globs.
- Consumes / produces: activity v1, temporal v1, stages v1, the level 0 vectors.
  Produces the stubs of section 5.
- Acceptance criteria:
  - [ ] `uv lock --check` exits 0, `uv export --package wdf-worker --extra <pool>` lists `uv` for compile only and `wdf-aggregate` for aggregate only, and every pool lists the `wdf-workflow` chain of D1.
  - [ ] `pytest packages/worker -v -k "stubs or wheel or config or stage_contract"` passes, and the stage contract test fails when the worker reads `memory_mib` or `cpu_cores` of the request.
  - [ ] `pytest packages/worker -v -m integration -k contract_cases` seeds a dev server and SeaweedFS pair and returns cases that `activity_contract` accepts against `FakePool` on all three pools.
  - [ ] `scripts/export-diagrams.sh --check` exits 0.
- Verifier: `test_stubs.py`, `test_wheel.py`, `test_config.py`, `test_stage_contract.py`, `test_contract_cases.py`.

### S11 The worker in the slot

Lane S, spike, timebox 1.5 days, tier T1 (Docker, no cluster), throwaway code in `docs/delivery/spikes/S11/code/`.
Needs: nothing.
Blocks: D.2, and `WORKER_RESERVE_MIB` in T.1.

- Question: does the worker process fit beside the warm-up and a warm task of the real workload component in each `ci` slot, and how much does it hold?
- Pass line, on 3 of 3 runs in `docker run --memory <slot> --memory-swap <slot> --cpus <slot cpu>` with the pool's Python, the worker's imports and a store client connected:
  - (1) simulate, 384 MiB and 250m: the worker's own peak is measured first and gives `WORKER_RESERVE_MIB` by the rule below, then the cold warm-up of the workload library component built by `wdf_compile`, then a warm `echo` task at the guest cap that reserve gives through 0.4's formula, all complete with no OOM kill, and the peak working set, the worker's own peak resident memory and the warm-up seconds are recorded;
  - (2) compile, 1,024 MiB and 1 CPU: the worker beside a build of the workload library bundle completes, with the peak recorded;
  - (3) aggregate, 1,024 MiB and 1 CPU: the worker beside `wdf_aggregate run` over the largest result the staging contract allows completes, with the peak recorded.
  - Any of the three failing is a fail of that pool.
- The result also records `WORKER_RESERVE_MIB` as the worker's peak plus a quarter in whole 64 MiB steps, the sizing rule of S8, and the figures of D18 that a container can show.
- On fail of (1): the result names the smallest simulate slot in 128 MiB steps that passes, and the spike worker files the `revisit` child of #2 of Q1 (a), which blocks D.2's simulate pod.
  A fail of (2) or (3) is not planned for, because S1b's 417 MiB peak leaves room in 1,024 MiB, so it is raised as needs-decision before D.2, and this spec's budget (D17, section 8) covers only the simulate step.
- Result: `docs/delivery/spikes/S11/worker-in-slot.md`.

### R.1 Runner

Lane R, tier T0, and T1 for the real `wasmtime`.
Needs: lane 0.
Blocks: T.1, T.2, T.3, A.1.

- Scope: `wdf_worker.runner.Child` of 0.4 with the process group, `PR_SET_PDEATHSIG`, the line reader and tail of D5, the group stop, and the OOM fact of D8 from the container's `memory.events`.
- Owns: lane R's globs.
- Consumes / produces: `wdf_runtime.heartbeat.beats`, the staging tail cap.
- Acceptance criteria:
  - [ ] `pytest packages/worker/tests/runner -v` passes, including `test_stop_kills_grandchildren` (a child that forks a sleeping grandchild leaves no process after `stop()`), `test_parent_death_kills_child` (a worker process killed with `SIGKILL` leaves its child dead within 1 s), `test_heartbeat_lines_parsed_live` (a beat is seen before the child exits), `test_tail_keeps_last_bytes`, `test_long_line_without_newline` (1 MiB of output with no newline ends with a 65,536-byte tail and no error) and `test_oom_fact_from_memory_events` (a fixture `memory.events` whose `oom_kill` rises gives `oom=True` on a `SIGKILL` and on a plain non-zero exit alike, and an unchanged count gives `oom=False`).
  - [ ] `pytest packages/worker/tests/runner -v -m integration -k wasmtime` runs a warm-up and a `wasmtime` guest of the workload component through the runner and stops a `cpu_burn` guest within 6 s of `stop()`.
- Verifier: `packages/worker/tests/runner/`.

### T.1 Simulate path

Lane T, tier T1 (SeaweedFS, real `wasmtime`).
Needs: R.1, and S11 for the value of `WORKER_RESERVE_MIB`.
Blocks: A.2.

- Scope: `wdf_worker.pools.simulate.run`: stage the component, write `input.json` from `params`, run the warm-up when `warmed` says no, run the guest with the cap of 0.4 and the deadline of D6, classify with `wdf_runtime.classify`, collect `results.xml`, upload the tail, return `TaskResult` or raise the class of D7.
- Owns: `packages/worker/src/wdf_worker/pools/simulate.py`, `packages/worker/tests/pools/test_simulate.py`.
- Consumes / produces: runtime v1, artifact v3, activity v1.
- Acceptance criteria:
  - [ ] `pytest packages/worker/tests/pools -v -m integration -k simulate` passes, including `test_echo_matches_level0_vector`, `test_first_run_warms_then_runs_warm` (one warm-up per component, and the second task runs none), `test_guest_memory_cap_deterministic`, `test_timeout_class`, `test_output_over_cap_guest_failed`, `test_input_modified_guest_failed`, `test_store_unavailable_class`, and `test_every_failure_type_is_a_table_row`.
- Verifier: `test_simulate.py`.

### T.2 Compile path

Lane T, tier T1 (SeaweedFS, real `componentize-py` and `wasm-tools`).
Needs: R.1.
Blocks: A.2.

- Scope: `wdf_worker.pools.compile.run`: stage the source bundle, run `python -m wdf_compile.limits fetch` with the mirror and then `python -m wdf_compile build --find-links DIR` with the deadline of D6, classify per D7, collect `component.wasm` and `build-info.json`, upload the tail.
- Owns: `packages/worker/src/wdf_worker/pools/compile.py`, `packages/worker/tests/pools/test_compile.py`.
- Consumes / produces: compile v1, artifact v3, activity v1; at T1 the mirror is the local wheelhouse directory, which the workload library bundle does not need.
- Acceptance criteria:
  - [ ] `pytest packages/worker/tests/pools -v -m integration -k compile` passes, including `test_workload_bundle_builds_with_its_cache_key`, `test_missing_entry_compile_failed_not_retried`, `test_library_error_is_worker_shutdown`, `test_timeout_compile_timeout`, and `test_stop_ends_uv_and_componentize`.
- Verifier: `test_compile.py`.

### T.3 Aggregate path

Lane T, tier T1 (SeaweedFS).
Needs: R.1.
Blocks: A.2.

- Scope: `wdf_worker.pools.aggregate.run`: write the index reference of `inputs` to a file, run `python -m wdf_aggregate run` with the pool's credentials, stop it at the deadline of D6 as `aggregate_timeout`, map its codes per D7, collect `summary.json`, upload the tail.
- Owns: `packages/worker/src/wdf_worker/pools/aggregate.py`, `packages/worker/tests/pools/test_aggregate.py`.
- Consumes / produces: aggregate v1, artifact v3, activity v1.
- Acceptance criteria:
  - [ ] `pytest packages/worker/tests/pools -v -m integration -k aggregate` passes, including `test_digest_list_over_two_results`, `test_malformed_block_aggregate_parse_failed`, `test_deadline_aggregate_timeout`, and `test_progress_lines_reach_heartbeat_state`.
- Verifier: `test_aggregate.py`.

### A.1 Activity, heartbeats and cancel

Lane A, tier T1 (dev server).
Needs: R.1.
Blocks: A.2.

- Scope: `wdf_worker.activity.run_task` with the dispatch of 0.4, the heartbeat task of D4, the cancel of D9, the worker-error mapping of D7, and the stage contract of D20.
- Owns: `packages/worker/src/wdf_worker/activity.py`, `packages/worker/tests/activity/**`.
- Consumes / produces: activity v1, temporal v1; the pool paths through lane 0's stubs, replaced by scripted handlers in its tests.
- Acceptance criteria:
  - [ ] `pytest packages/worker/tests/activity -v -m integration` passes, including `test_heartbeats_during_silent_child` (beats arrive within a 5 s heartbeat timeout while a child prints nothing for 20 s), `test_cancel_stops_child_and_withdraws` (the `run_task_once` result is a cancel, and no child is left), `test_worker_error_is_worker_shutdown` (an exception message of 10,000 characters arrives as a reason of at most 1,024), and `test_heartbeat_details_validate`.
- Verifier: `packages/worker/tests/activity/`.

### A.2 Service and conformance on every pool

Lane A, tier T1 (dev server and SeaweedFS, marked `pair`).
Needs: A.1, T.1, T.2, T.3.
Blocks: D.2.

- Scope: `python -m wdf_worker --pool <pool>` per 0.4 with the drain of D10 and the readiness file of D14, `wdf_worker.probe`, and `activity_contract` against the real worker on each pool.
- Owns: lane A's remaining globs.
- Consumes / produces: `activity_contract`, `contract_cases`.
- Acceptance criteria:
  - [ ] `pytest packages/worker/tests/service -v -m integration -k activity_contract` passes on `wdf-compile`, `wdf-simulate` and `wdf-aggregate` behind a per-test prefix of D22, each against a real `python -m wdf_worker` process started with that `WDF_POOL_QUEUE_PREFIX`.
  - [ ] `pytest packages/worker/tests/service -v -k "sigterm_drains or sigterm_fails_long_task or cancel_during_drain_withdraws or missing_env_exits_2 or ready_file"` passes, with a task longer than the drain ending as `worker_shutdown` and a server cancel during the drain ending as a withdrawn attempt.
- Verifier: `packages/worker/tests/service/`.

### D.1 Images

Lane D, tier T1 (Docker build).
Needs: lane 0.
Blocks: D.2.

- Scope: the Dockerfile and `image.sh <pool>` of D16.
- Owns: `deploy/workers/{Dockerfile,image.sh,test_image.py}`.
- Consumes / produces: `images.python`, the dev shell tools.
  Produces three local images.
- Acceptance criteria:
  - [ ] `deploy/workers/image.sh simulate` builds, and `docker run --rm --entrypoint wasmtime <image> --version` prints `wasmtime 48.0.7`, and `image.sh` fails on a changed `hash` in a copy of `nix/wasm-tools.nix`.
  - [ ] `pytest deploy/workers -v -k image` passes: the compile image runs `wasm-tools --version` and `uv --version` with the dev shell's versions, `python -m wdf_compile build` of the workload library bundle succeeds under `docker run --read-only --user 65532` with a volume at `/var/wdf` and the environment of D13, the aggregate image has no `wasmtime` and no `wasm-tools`, the simulate image has no `wasm-tools` and no `uv`, each image's command is `python -m wdf_worker --pool <its pool>`, each image runs as user 65532, and none holds a file under `/etc/wdf`.
- Verifier: `deploy/workers/test_image.py`.

### D.2 Pool modules

Lane D, tier T2.
Needs: D.1, A.2, S11 (and the Q1 revisit on its fail), H.1 for the T2 rows.
Blocks: the exit test.

- Scope: the three modules of 0.4 with D2, D10, D11, D12, D13, D14 (the readiness probe), D15, the pod settings of D16 (`runAsUser: 65532`, `readOnlyRootFilesystem: true`, `automountServiceAccountToken: false`) and the NetworkPolicies of Q2, the `TREES` entry, and the T2 checks below.
- Owns: `deploy/workers/{compile,simulate,aggregate}/**`, `deploy/workers/{secrets.py,policy.py,test_render.py}`, the `TREES` line.
- Consumes / produces: module v1, topology v1, store v3.
  Produces the modules.
- Acceptance criteria:
  - [ ] `pytest deploy/workers -v -k render` passes: requests equal limits equal the slot, `replicas: 1`, `Recreate`, the pool's selector and toleration, a Secret with exactly one identity, no `WDF_*_DSN`, no `WDF_POOL_QUEUE_PREFIX` and no deployment or build id variable, a disk `emptyDir` at `/var/wdf` with the environment of D13, the pod settings of D16, the readiness probe, and the egress of Q2.
  - [ ] T2: `just up` then `scripts/tilt/run.sh ci` exits 0 with the three Deployments `Available` in `wdf-workers`, and each pod's log shows its queue polled with no deployment version.
  - [ ] T2: `kubectl --context kind-wdf -n wdf-workers exec deploy/compile-worker -- python -m wdf_worker.probe` prints `mirror: ok` and `appdb: refused`, and the same in `simulate-worker` prints `mirror: refused`.
  - [ ] T2: `python -m wdf_workflow.harness --kube-context kind-wdf smoke --pools real --jobs 5` prints `finished: 5`, `failed: 0`.
- Verifier: `deploy/workers/test_render.py`, the T2 commands.

### H.1 Harness drives real pools (revisit of #6)

Lane H, tier T1, and T2 in D.2 and the exit test.
Needs: nothing.
Blocks: D.2.

- Scope: Q4 (a): `--pools real` on `smoke` and `restart`, which starts no `FakePool`, seeds the workload library bundle with entry `wdf_workloads.main`, uses level 0 `echo` params and aggregation `digest_list`, checks each simulate result against `contracts/workload/vectors/level0/echo/expected.json` and prints `verified: N`, the number of jobs whose four simulate results (`SIMULATE_TASKS`, `harness/__init__.py:74`) all match, and accepts `compile-worker`, `simulate-worker` and `aggregate-worker` in `--target`.
  The queue prefix of D22: `--queue-prefix P` on `smoke` and `restart`, defaulting to a fresh prefix per run with `--pools fake` and to none with `--pools real`, passed to every `FakePool` through `queue` and to every job start as the memo `wdf_pool_queue_prefix`, and the memo read in `wdf_workflow.taskpath` that applies it to each placement.
  With real pools, `restart` runs a level 1 `cpu_burn` workload long enough to outlast a restart, finds a simulate task in flight from the job's status query instead of `fakes["simulate"]` (`harness/__init__.py:643`), deletes the pod with `--grace-period=0 --force` so the drain of D10 cannot finish the task, and counts infrastructure retries from every job's status instead of `FakePool` calls (`harness/__init__.py:574-597`).
  The default `--pools fake` keeps every existing scenario as it is, on its own prefix.
  The dated Closure addendum line of `08-workflow-engine.md` links this issue and records that the workers spec drops the worker's cache re-check that `08-workflow-engine.md:319` keeps (decision 7).
- Owns: lane H's globs.
- Consumes / produces: `wdf_workloads.bundle`, the level 0 vectors.
- Acceptance criteria:
  - [ ] `pytest packages/workflow/tests/harness -v` passes, including the existing tests unchanged and `test_real_pools_starts_no_fake`, `test_real_pools_seeds_workload_bundle` and `test_fake_run_gets_its_own_prefix`.
  - [ ] `pytest packages/workflow -v -k "queue_prefix or replay"` passes: a job started with the memo of D22 runs on `FakePool`s of the prefixed queues while `FakePool`s on the contract names take no task, and the replay corpus replays unchanged.
- Verifier: `packages/workflow/tests/harness/`.

## 5. Order, stubs and spikes

- First, in parallel: 0.1, S11 and H.1.
  0.1 merges alone, with an estimate of 1.5 days.
- After 0.1: R.1 and D.1.
  After R.1: T.2, T.3 and A.1, and T.1 once S11 has also merged.
  After A.1 and the three task paths: A.2.
  After A.2, D.1, S11, H.1 and, when S11 failed, the Q1 revisit: D.2.
  Then the exit test.
- DAG: 0.1, S11 and H.1 start together, R.1 and D.1 wait for 0.1 only, the T lane and A.1 wait for R.1 (T.1 also for S11), A.2 waits for A.1 and the T lane, D.2 for A.2, D.1, S11 and H.1, then the exit test.
- Cluster work: D.2's T2 rows and the exit test, each under the cluster lock, one at a time.
  The capacity revisit of #2 has landed (`c8fa830`), and the exit test reads the preset size at the commit it runs.

Stubs:

| Stub | What it fakes | How its conformance test runs against the real thing |
| --- | --- | --- |
| `wdf_worker.pools.<pool>.run` raising `NotImplementedError` | the task paths of lane T, for A.1 | A.2 runs `activity_contract` against the real worker on each pool |
| `run_task_once` per task | `JobWorkflow` driving real pools, until H.1 merges | D.2 and the exit test run the harness with `--pools real` |
| `FakePool` (existing) | these workers, for #6's own tests and for fake runs on a prefix of D22 | `activity_contract`, the same test on the same `run_task` interface, now passes against both |

Spikes: S11, as its block in section 4 states, runs beside lane 0 and gates D.2.

## 6. Test delta

Tests: each issue in section 4 names its tests and tier, T0 and T1 under `packages/worker/tests/` and `deploy/workers/`, and T1 under `packages/workflow/tests/` for H.1, all run by `just test`, `just itest` and `just ci`.
Commands: `python -m wdf_worker`, `python -m wdf_worker.probe`, `deploy/workers/image.sh <pool>`, and the harness options `--pools real` and `--queue-prefix`.
No helper script, verifier skill, feature file or verify recipe (decision 1).
What stays untested here is listed under "Not provable by this epic" in section 7, and the exit test proves the epic from a clean export.

## 7. Exit test

Run by a worker who did not write the lanes, on a clean `git archive` export of `main` taken outside any git repository, on the 20 GiB WSL2 host with Docker running for the whole run.
Containers other than the ones the run starts are never touched.
Each command runs from the export root, and its real output is the evidence file `exit-test/row-<#>.txt` beside the export, pasted in the PR that records the run and summarised in Closure.
Rows 6 to 11 take the cluster lock for their whole sequence, and row 12 runs outside it.
`H` stands for `python -m wdf_workflow.harness --kube-context kind-wdf`.

| # | Command | Expected |
| --- | --- | --- |
| 0 | `grep -E '^(MemTotal\|MemAvailable):' /proc/meminfo` and `docker ps --format '{{.Names}}'`, repeated just before row 6 | `MemAvailable` at least the `memory_mib` of `deploy/presets/ci.yaml` at the run's commit plus 1,024 MiB, in kB (13,369,344 kB at the 12,032 MiB of the S11 revisit, https://github.com/AndoNorth/wasm-dag-forge/issues/380), else the run stops and records the figure |
| 1 | `uv sync --frozen` then `uv lock --check` | exit 0 for both, lock unchanged |
| 2 | `pytest packages/worker -q -rs` | passes with no `SKIPPED` line |
| 3 | `pytest packages/worker deploy/workers -v -k "stop_kills_grandchildren or parent_death_kills_child or heartbeats_during_silent_child or cancel_stops_child_and_withdraws or first_run_warms_then_runs_warm or echo_matches_level0_vector or every_failure_type_is_a_table_row or library_error_is_worker_shutdown or deadline_aggregate_timeout or activity_contract or sigterm_fails_long_task or stage_contract or render or image"` | each named test is listed `PASSED`, `activity_contract` once per pool |
| 4 | `pytest packages/workflow/tests/harness -v` | passes, with the H.1 tests listed |
| 5 | `scripts/check-contracts-map.sh`, `scripts/export-diagrams.sh --check`, `just versions --check`, then the pool worker diagram opened | exit 0 for each, and the diagram shows the `run_task` path of 0.1 with no versioned queue and no checkpoint step |
| 6 | `just up`, `scripts/tilt/run.sh ci`, `kubectl --context kind-wdf -n wdf-workers get pods -o wide`, `kubectl --context kind-wdf -n wdf-workers logs deploy/<pool>-worker` for each pool | exit 0, the three worker pods `Running` on the compile, simulate and aggregate nodes, and each log showing its queue polled with no deployment version |
| 7 | `kubectl --context kind-wdf -n wdf-workers exec deploy/<pool>-worker -- python -m wdf_worker.probe` for each pool | the answers of Q2: the mirror only from the compile pod, the app database from none |
| 8 | `H smoke --pools real --jobs 10`, then `H smoke --jobs 5` | `finished: 10`, `failed: 0`, `verified: 10` (jobs, each with four matching simulate results), then, on fake pools behind a fresh prefix of D22 beside the deployed workers, `finished: 5`, `failed: 0` |
| 9 | `H restart --pools real --jobs 10 --target simulate-worker` | `finished: 10`, `failed: 0`, `infra_retries:` at least 1, from a pod deleted with no grace while a `cpu_burn` simulate task runs |
| 10 | `kubectl --context kind-wdf -n wdf-workers exec deploy/<pool>-worker -- cat /sys/fs/cgroup/memory.events /sys/fs/cgroup/memory.stat /sys/fs/cgroup/memory.peak` for each pool | `oom_kill 0` on every pool, and `anon` and `memory.peak` recorded in Closure, where `memory.peak` is shown for reference only because it counts page cache and reaches the cap on every first run (`docs/delivery/spikes/S6/cold-start.md:72`) |
| 11 | `just doctor`, then `just down`, then `just nuke` twice | exit 0 for each, and `just nuke` leaves nothing and repeats cleanly |
| 12 | `just ci` | exit 0, on the full path |
| 13 | the `ci` and `state-modules` workflows on `main` | one green run of each, the URLs recorded in Closure |

Not provable by this epic: checkpoints, `operator_interrupt` and resume from a checkpoint (#18), OOM classification at the pod limit and the kubelet setting (#15), worker metrics and JSON logs (#13), real placement and the pool table (#9), node loss and an engine outage longer than the heartbeat timeout (#15), module cache growth over a soak (#16), and the `dev`, `full` and `soak` profiles.

## 8. Parallelism and cost

- Maximum parallel workers: 3 in the first phase (0.1, S11, H.1), then 2 (R.1, D.1), then 3 (T.1, T.2 and T.3 by two workers, and A.1), then 1 for A.2, D.2 and the exit test.
- Host constraints: T1 work holds at most two containers across the host, and S11 one capped container at a time.
  The cluster is single-owner, so D.2's T2 rows and the exit test queue for the lock, and `ci` needs 12,032 MiB of the 20 GiB host beside other containers (`deploy/presets/ci.yaml:8`), the simulate step of Q1 included.
- CI cost, estimates: no new workflow, one dev server and one SeaweedFS per `just itest` session, a workload build of about 8 s at T1 (S1b measured 7.7 s), and a warm-up of about 2 s on the uncapped host (S6).
- Estimates, not measurements, in worker-days: 0.1 (1.5), S11 (1.5), H.1 (1.5), R.1 (2.5), T.1 (2), T.2 (2), T.3 (1.5), A.1 (2), A.2 (2), D.1 (2), D.2 (3), exit test (1.5).
  That is about 23 worker-days of effort and about 14 days elapsed along 0.1, R.1, T.1, T.3, A.2, D.2 and the exit test (1.5 + 2.5 + 2 + 1.5 + 2 + 3 + 1.5, with A.1 and T.2 inside the T lane's window), with S11 failing into the simulate resize of Q1, the NetworkPolicy fallback of Q2, a real component larger than S6's stand-in and cluster lock contention as the main sources of slip.

## 9. Closure (filled at close)

Not run yet.

Closure addenda, one dated line each, added after close when a `revisit` child issue is filed against this epic:

- none yet
