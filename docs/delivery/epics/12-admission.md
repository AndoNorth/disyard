# Epic #8: Job admission scheduler (layer 1) - breakdown

Status: draft, reconciled on 2026-11-17 with the built broker (#7) and workers (#10) on `main` at `331e013`, then on 2026-11-17 with the spec review on `main` at `2956cf1`: its findings are applied and the captain's answers to Q2 to Q5 are recorded, awaiting the captain's approval
Epic: https://github.com/AndoNorth/wasm-dag-forge/issues/8
Architecture note: docs/architecture/17-admission-cycle.drawio, added with this spec, and docs/architecture/10-intake-and-admission.drawio, which 0.1 (issue) refines
Verifier: none, per decision 1.
Depends on (closed epics): Dev environment (#1), Fixed-resource cluster (#2), Job event envelope (#4), DAG configuration schema and validator (#5), Durable state and artifact storage (#3), Artifact passing (#12), Workload toolchain (#11), Workflow engine (#6), Broker (#7), Kubernetes workers (#10), and the closed revisits #366 (the `ci` capacity) and #380 (the simulate slot) of #2 and #374 (the harness queue prefix) of #6
Depends on (open work): none. The dispatch and control API (#24) is specified and not built, the tests and the driver stand in for it (D19), and section 2 lists what this spec assumes of it.

Evidence marks: `[read]` means read on `main` at `6439283` on 2026-11-15 while preparing this spec, or at `331e013` on 2026-11-17 while reconciling it with the built #7 and #10, or at `2956cf1` on 2026-11-17 while applying the spec review, and nothing was executed beyond read-only commands, `scripts/cluster/budget.sh ci` and `pytest scripts/cluster/test_budget.py`.
The epic issue body was read on 2026-11-15, it has no comments, and it was last edited on 2026-10-27, before the workflow epic was specified.
Where the body and the landed code or the pinned contracts disagree, the landed code and the contracts win, and section 1 lists each case.
Method: [docs/delivery/method.md](../method.md).
Model: [docs/delivery/epics/08-workflow-engine.md](08-workflow-engine.md), whose structure this spec follows.
Tiers: T0 is host Python with no runtime, T1 is containers started through `wdf_itest` (Postgres, the Temporal dev server of the pinned CLI image and RabbitMQ 4.4.0 at the digest #7 pins, `deploy/versions.yaml:38-41`, at most two containers across the host, one per host-wide lock file of `wdf_itest`, `packages/itest/src/wdf_itest/session.py:21,48-50`), and T2 is the cluster from `just up` on preset `ci`.
A test that needs a container carries the `integration` marker and runs in `just itest` and in every lane's `just ci`.
Command prefixes `nix develop -c` are omitted from every command of this spec and every command takes them, and every Python command also takes `uv run --frozen`.
T2 commands run under `flock /tmp/wdf-cluster.lock env WDF_CLUSTER_LOCK_HELD=1 ...` as the cluster epic says.

## 0. Decisions

Section 0.1 lists what is already decided and carried, 0.2 the questions, all five decided, 0.3 the defaults that the lanes build on, and 0.4 the proposals that lane 0 and the issues pin.
Every decided answer in 0.2 and every default in 0.3 keeps every pinned contract as it is, except the receipt write of D9, which W.1 brings to the stage and state contracts.
A change to a recorded decision changes only what its Applied in column names, and a change to a default only what its Changes column names.
"0.1 (issue)" names the lane 0 issue of section 4, and a bare 0.1 to 0.4 names a part of this section.

### 0.1 Recorded decisions

| # | Decision | Source | Applied in |
| --- | --- | --- | --- |
| 1 | No verifier skill, no feature files and no verify recipes for this epic, and no verifier row of the method applies to #8. Every issue names its tests and the exit test is a command checklist | the standing rule of every executed epic (`08-workflow-engine.md:32`), and the realignment of 2026-11-13, which decided to retire `just verify` and `just verify-clean` and gave `just cluster-verify` to the capacity revisit of #2, carried out by `c8fa830`: `just/verify.just:1-2` holds only `verify-lint`, `just/cluster.just:10-11` runs the real `scripts/cluster-verify.sh`, and the method records the verifier convention as retired (`docs/delivery/method.md:149-154`) [read] | sections 3, 4, 6 |
| 2 | Runtime libraries read contract files through package data and never by repository path, and the package has a wheel test | state epic decision 11, carried by every later epic (`08-workflow-engine.md:34`) | 0.1 (issue), every package issue |
| 3 | The start contract is the #6 rules row for #8: commit the move to `admitted` before the start (D18 of #6), start `JobWorkflow` on `wdf-workflow` with `wdf_keys.workflow_id(job_id)`, `REJECT_DUPLICATE`, the five search attributes, and the accepted event and the params block as the two arguments, and send `resume` with the row's `control_seq` and pending patch only while the row's `requested_action` is `resume` (D19 of #6). This replaces the body's "workflow id derived from tenant and submission key" and its proposed attribute names | `08-workflow-engine.md:298`, `contracts/temporal/naming.yaml:4-12,40-45`, `packages/workflow/src/wdf_workflow/job.py:143` [read] | C.1, S.1 |
| 4 | The params block is `wdf_events.models.JobParams` built from the row's `seed` and `parameters`, with `class_override` null, because the row has no such column and the workflow falls back to the accepted event's `jobclass` | #6 decision 4 (`08-workflow-engine.md:35`), `packages/events/src/wdf_events/models.py:229-239`, `job.py:65-70` [read] | C.1 |
| 5 | Admission learns of a pause from the job row that the workflow projects, and a granted resume is signalled to the paused workflow by admission and by no one else | #6 decision 8 (`08-workflow-engine.md:39`), and the #24 rules row, which sends only `cancel`, `terminate` and `pause` (`:299`) [read] | S.1, S.2 |
| 6 | The priority class vocabulary is `standard` only for the proof of concept, as every vector and test uses it, and tiers come later | realignment answer D5 of 2026-11-13, `contracts/events/vectors/golden/job.admitted/1.0.json` [read] | P.1, 0.4 |
| 7 | Dead-letter reasons: `INVALID_DAG` is dropped, `UNKNOWN_TENANT` is a rejection at the API edge only, and `job.dead.lettered` 1.0 keeps its two reasons, `MALFORMED` and `UNSUPPORTED_VERSION`. A dead letter by delivery limit needs a signal from which the job can be recovered, which question Q1 settles. This answers the epic's open question 2 | realignment answer D6, `contracts/events/schemas/job.dead.lettered/1.0.json:12-17` [read] | C.2, Q1 |
| 8 | Admission re-admits a job that was resumed before it started, whose intake message was already consumed, and grants it in its tenant's round-robin turn ahead of that tenant's queue (0.4 Policy), so a tenant cannot jump the other tenants by pausing and resuming its own backlog. A row that stays `admitted` with no workflow already holds its slot, so the scan starts it without a grant | realignment answer D8, with the order refined by the spec review of 2026-11-17 | S.2, P.1 |
| 9 | The job class is declared by the submitter with the default `short` and lives in the row, which this epic reads. This answers the epic's open question 3 | state epic decision 8 (`05-state-storage.md:34`), `packages/jobstate/src/wdf_jobstate/models.py:33` [read] | P.1, C.1 |
| 10 | The group role `wdf_admission` cannot log in, so the admission module creates its own service login in that group, in the way `deploy/workflow/logins.sh` and `logins.py` do for the workflow worker | `packages/stateops/src/wdf_stateops/migrations/R__roles.sql:5`, `05-state-storage.md:162` ("Login roles are the service epics'"), `deploy/workflow/logins.py:24-29` [read] | M.2 |
| 11 | Admission writes `job.admitted` in the transaction of the claim, `job.published` in that transaction when the claim set a missing receipt (D9, W.1), and `job.dead.lettered` for a message refused as `MALFORMED` or `UNSUPPORTED_VERSION` that names a job, all from source `/wdf/admission`, and the real stage passes `run_stage_contract("admission", ...)` | `contracts/stages/stages.yaml:27-36`, `contracts/state/README.md:158`, `packages/stages/src/wdf_stages/contract.py:85` [read], and the stage contract change of W.1 | C.1, C.2, W.1 |
| 12 | The Temporal client is set up as the workflow service sets it up, with `event_data_converter` and `tracing_interceptor()`, and the start runs inside `stage_span(tracer, "admission", traceparent)` so the trace context of the row reaches the start headers | `packages/workflow/src/wdf_workflow/service/__init__.py:159-163`, `packages/temporal/src/wdf_temporal/trace.py:32,39` [read] | C.1 |
| 13 | Placement is on the proof-of-concept path through #9 after #10, and admission keeps the body's "no coupling in v1" with it | realignment answer D2 | none |
| 14 | The `ci` capacity revisit of #2 (#366) has landed: the infra node is 6,144 MiB and `scripts/cluster/budget.sh` holds a reserve row `admission` of 50m and 128 MiB requested, 250m and 128 MiB limits, which it drops once a `module.yaml` named `admission` exists, so no lane waits for it | realignment answer D4, the capacity scout of 2026-11-13, landed by `c8fa830` (https://github.com/AndoNorth/wasm-dag-forge/pull/371): `deploy/presets/ci.yaml:15-18`, `scripts/cluster/budget.sh:35-39` [read] | M.2, section 5 |
| 15 | Dedup happens once, at submit, under the unique constraint of #24, and the workflow id with `REJECT_DUPLICATE` stays a backstop at the start, so `WorkflowAlreadyStartedError` is a harmless repeat | the body's key decisions, `naming.yaml:9` [read] | C.1 |
| 16 | The epic body is refreshed as this breakdown starts; this spec records what in it is stale (section 1), and the body edit itself is not part of this spec's pull request | realignment answer D10 | none |
| 17 | The pool workers of #10 poll the contract queues `wdf-compile`, `wdf-simulate` and `wdf-aggregate` on every `ci` cluster, so admission starts a real job with no memo, and a run on `FakePool`s puts its pool queues behind a prefix of its own through the memo `wdf_pool_queue_prefix` of the start, which the workflow applies to each placement (#10's D22). Q5 settles how the driver's prefix reaches admission's start. This replaces the draft's `FakePool`s on the contract names, where a real worker would take a driver task and fail it | `11-workers.md:84` (D22) and `:203`, `packages/workflow/src/wdf_workflow/taskpath.py:52,222-224`, #10's exit test row 8 (`11-workers.md:527`) [read] | Q5, D15, D19, 0.4, C.1, M.1, M.2, V.1, section 7 |
| 18 | The broker behaviours that this draft left open are measured by S4b and built into #7: the policy `wdf-intake` spaces each counted retry by 1 s times its delivery count, at most 30 s, so admission sends `retry()` at once and keeps #7's rules row for #8 in full (A4, D7); a delivery held when its connection drops, its client closes or the broker restarts comes back with one delivery spent, and a held last delivery dead-letters as `delivery_limit`, so C.2 drops its `crash_redelivery_count` measurement and D18 releases every held delivery before it closes the connection; and `release()` puts a message back ahead of every message never delivered, with its count unchanged, so C.2's `class_at_cap_released` asserts it instead of printing it (Q4) | `docs/delivery/spikes/S4b/broker-client-py313.md:97-109` (check 6), `:116-124` (check 7) and `:179-187` (check 13), `contracts/broker/topology.yaml:19-23`, `packages/broker/src/wdf_broker/client.py:105-109,280-281` [read] | Q1, Q4, A4, D7, D18, C.2, section 5 |
| 19 | #7 pins the field `caps.open` and every `ci` figure that D1 asked for, each an `Estimate` read through its `value`, so admission drops its fallback of the open cap to `caps.global` | `contracts/tenancy/tenancy.yaml:8-18`, `10-broker.md:110` (#7's D3), `packages/broker/src/wdf_broker/models.py:46-50,219-247` [read] | D1, A3, 0.4 |
| 20 | Lane 0 writes no broker fake and no `wdf_admission.testing`, because #7 ships `FakeBroker`, `broker_contract` and `broker_container` (A5), and lane 0 declares `wdf-broker` as a dependency from the start | `packages/broker/src/wdf_broker/testing/` [read] | 0.1 (issue), sections 3 and 5 |
| 21 | M.2 also edits `scripts/cluster/test_budget.py`: it moves `admission` out of the reserve loop of `test_ci_infra_node_figures`, which asserts that the reserve row is printed, because the budget check drops that row once `deploy/admission/module.yaml` exists. The node figures that the test asserts stay, since the module's requests equal the row. #24's D.2 edits the same line for `dispatch`, so whichever lands second rebases onto the first | `scripts/cluster/test_budget.py:40-49`, `scripts/cluster/budget.sh:32-39`, `13-dispatch-api.md:59,410` [read] | section 3, M.2 |

### 0.2 Questions

Five questions, all decided: Q1 through #7's review, and Q2 to Q5 by the captain accepting their recommended answer in the spec review of 2026-11-17.
Each decided answer keeps every pinned contract as it is.

**Q1. What signal does a delivery-limit dead letter leave behind?**
A message whose handling keeps failing for a reason that is neither `MALFORMED` nor `UNSUPPORTED_VERSION` (an unexpected error, not an outage, since D7 releases a message uncounted during an outage) is retried as a counted delivery, which the broker redelivers after 1 s times its delivery count (the delayed retry of `wdf-intake`, `contracts/broker/topology.yaml:19-23`), and RabbitMQ dead-letters it after the fifth retry with only the broker reason `delivery_limit` (`docs/delivery/spikes/broker-client.md:50-52`).

- (a) Recommended: on the last delivery the limit allows (`Delivery.last` of #7's client, true when `delivery_count` is 5, on the sixth delivery, `packages/broker/src/wdf_broker/client.py:80`), admission dead-letters the message itself with the reason `ADMISSION_FAILED` and the detail token of the error class, counts it in `wdf_admission_dead_lettered_total{reason="ADMISSION_FAILED"}`, logs the job id, and leaves the row at `accepted`, so the replay tool of #7 can admit it again (`contracts/status/status.yaml:49`).
  No event is written, because `job.dead.lettered` 1.0 cannot carry the reason.
  The broker's own `delivery_limit` death then remains only for a message whose last delivery is lost with the admission process or its connection, and the replay tool shows it with that broker reason.
  S4b measured that such a loss counts: a delivery held when its connection drops, its client closes or the broker restarts comes back with one delivery spent, and a held last delivery dead-letters as `delivery_limit` (`docs/delivery/spikes/S4b/broker-client-py313.md:97-109`, check 6).
  So an admission process that dies six times while it holds a message, in an out-of-memory loop for one, dead-letters that message with no event, at most the grants of one cycle at a time (D2, D3), and the `delivery_limit` row of `contracts/broker/reasons.yaml` recovers it by replay.
- (b) A contract-change PR on the events contract: `job.dead.lettered` 1.1 adds `ADMISSION_FAILED` to `dlq_reason`, admission also writes the event, and the event history and the status read of #24 show the cause.
  Consumers #7, #13 and #24 take the minor bump.
- (c) Never count a retry: admission releases every failed delivery uncounted, so no message is dead-lettered by limit, and a poison message loops in its queue until an operator removes it.

Decided: (a).
The captain accepted option (a) of the same question in #7's review on 2026-11-15 (`10-broker.md:51-66`), #7's rules row for #8 carries it (`10-broker.md:254`), and `contracts/broker/reasons.yaml:20-26` pins `ADMISSION_FAILED` with no event and the recovery `replay` (assumption A4).
Option (b) stays available as a later events contract change, once #24's status must show a dead letter, as #7 recommends.
A failure after the claim, at the start, leaves the row at `admitted` instead of `accepted`, where the next delivery, a replay or the stuck-row scan starts it (D8, D9, D10).

**Q2. Is strict class order with the reserve built now, while `standard` is the only class?**

- (a) Recommended: P.1 builds strict class order, the one-in-five reserve for the lowest waiting class and tenant round-robin inside a class as pure functions over the ordered `priority_classes` of #7's tenancy profile, with property tests at T0 over a three-class fixture, and the shipped configuration lists only `standard`.
  The extra cost is about half a worker-day (an estimate), and tiers later need configuration only.
- (b) P.1 builds tenant round-robin over one class only, and class order with the reserve is built by the epic that adds tiers.

Decided: (a).
The captain accepted the recommended answer in the spec review of 2026-11-17, and P.1 builds it.

**Q3. Where does the live-job count come from?**

- (a) Recommended: from the job rows.
  A live job is a row in `admitted` or `running`, plus each resume that admission granted and whose row has not yet left `resume-pending`.
  Admission reads the counts at each cycle, so no result watcher per job and no recovery from Temporal visibility is built, and a restart needs no recovery step.
  The resumes granted before a restart are recovered from Temporal and not from memory: D12 counts a `resume-pending` row as granted once its workflow's `status` query shows the row's `control_seq` applied, or when the query fails.
  A second cap counts open jobs (`admitted`, `running`, `paused` and `resume-pending`), every open workflow plus the few jobs paused before they started, which have none and make the count err high, because a job that pauses itself when a checkpointing task runs out of retries (`packages/workflow/src/wdf_workflow/result.py:35-44,80-84`) stays an open workflow, so a pool outage would otherwise turn the backlog into paused workflows (D1).
  The row is what the workflow projects (#6 decision 6), admission already reads it for pauses (decision 5), and a paused job releases its slot the moment its row says `paused`.
  The cost is a scan of `accepted_job` per cycle, which has no index on `status` (D11 measures it).
- (b) The body's design: one result watcher per admitted job (up to the global cap of long polls in a 128 MiB process), Temporal visibility at start-up, and a drift check.
  Visibility counts a paused workflow as open, so the pause rule of decision 5 would still need the row.

Decided: (a).
The captain accepted the recommended answer in the spec review of 2026-11-17, and D11, D12 and S.2 build it.

**Q4. How is head-of-line waiting by job class handled?**
Intake queues are keyed by tenant and priority class, so a pulled job's job class is known only after the pull, while the caps are per job class.

- (a) Recommended: keep the queue key that #7 built, `intake.<tenant>.<class>` with the priority class as `<class>` (`contracts/broker/topology.yaml:8`).
  When the pulled job belongs to a job class at its cap, admission releases the message uncounted with `release()`, one of the four release causes of #7's Q4 (a), and skips that queue until the class has a free slot, through the blocked class that 0.4 Policy gives the planner, so short jobs of that tenant and priority class wait behind a long one.
  The released message goes back ahead of every message never delivered, with its count unchanged (`docs/delivery/spikes/S4b/broker-client-py313.md:179-187`, check 13), so the queue keeps its order.
  Nearly every proof-of-concept job is `short`, the default, and long jobs arrive with the checkpoint epic (#18).
- (b) A contract-change PR on broker v1 adds the job class to the queue key (`intake.<tenant>.<priority class>.<job class>`), with the pattern of the policy `wdf-intake` and the read permission of the `admission` user (`contracts/broker/topology.yaml:11,48`), #24 routes by it, admission pulls per job class with no head-of-line wait, and the number of queues doubles.

Decided: (a).
The captain accepted the recommended answer in the spec review of 2026-11-17, and P.1 and C.2 build it.

**Q5. How does the deployed admission put the driver's jobs on the driver's fake pool queues?**
Decision 17 keeps a run on `FakePool`s off the contract queues that the deployed pool workers poll, through a queue prefix in the memo of each start, and #10 asks this spec to name how its driver does it (`11-workers.md:203`).
The row holds no prefix, so admission must learn it from somewhere else.

- (a) Recommended: the service reads an optional `WDF_POOL_QUEUE_PREFIX` and, when it is set, starts every job with the memo `wdf_pool_queue_prefix` of that value (`QUEUE_PREFIX_MEMO`, `packages/workflow/src/wdf_workflow/taskpath.py:52`), as #10's worker service reads the same variable (`packages/worker/src/wdf_worker/config.py:26`).
  The module never sets it, and a render test refuses it, as #10's pool modules do (`11-workers.md:410`).
  The driver sets it on the Deployment to a fresh prefix of `wdf_workflow.harness.fresh_prefix()` (`packages/workflow/src/wdf_workflow/harness/__init__.py:279-281`) for its run, runs its `FakePool`s behind it, and removes it after, as it does `WDF_ADMISSION_GLOBAL_CAP`.
  Every job admitted during a driver run, a re-admission of D10 included, lands on the prefix, which the cluster lock that the driver holds already makes safe, and a row of an earlier driver run that a scan starts meanwhile lands there too.
  Each change of the Deployment is a rollout under `Recreate` (D16), so the driver waits for `kubectl rollout status deploy/admission` after it sets the variables and before it publishes, and again after it removes them.
  It removes both variables before it sets its own, so a run killed before its cleanup is undone by the next run, and it removes them in a `finally` that also runs on an interrupt or a lost port-forward.
  A run killed with no cleanup at all leaves them set until the next driver run or `kubectl -n wdf-control set env deploy/admission WDF_POOL_QUEUE_PREFIX- WDF_ADMISSION_GLOBAL_CAP-`, which the module README states.
- (b) The driver runs its journeys on the deployed pool workers with no prefix, seeding the workload library bundle and the level 0 `echo` params as the harness's `--pools real` does, and admission never sets a memo.
  It proves admission with real workers, at their measured pace of 10 jobs in 6.5 minutes (`11-workers.md:527`), so the 500 jobs of exit row 8 would take about 5.4 hours and every journey shrinks to tens of jobs, and the `control` journey can no longer script how long a task runs, as a `FakePool` lets it.
- (c) The row or the accepted event carries a prefix that admission copies into the memo: a state or events contract change for a test-only need, which #10's D22 rejected for `JobParams` (`11-workers.md:84`).

Decided: (a).
The captain accepted the recommended answer in the spec review of 2026-11-17, and D15, D19, M.1, M.2 and V.1 build it.

### 0.3 Defaults the lanes build on

These carry the remaining open questions of the epic and the choices found while reconciling it.
Every number marked as an estimate is fixed by the characterization epic (#16).

| # | Topic | Default | Reason, and the alternative | Changes |
| --- | --- | --- | --- | --- |
| D1 | Caps (open question 1) | The caps live in #7's tenancy profile (A3), which pins every `ci` figure this spec asked for, `caps.open`, `caps.global`, each tenant quota, `caps.job_class` and `reserve_interval`, each marked `estimate: true` (`contracts/tenancy/tenancy.yaml:8-18`, decision 19). At the open cap, queue grants and re-admissions stop and resume grants go on | The #6 exit test ran 2,000 open jobs on the bounded workflow worker within its 256 MiB (`08-workflow-engine.md:603,619`), so 2,000 is the largest proven figure of open workflows, and the live cap at half of it leaves the other half for paused jobs. Long jobs hold a slot for hours, so their cap is a tenth. A figure changes only by a value PR to the tenancy profile after #16 measures it (`10-broker.md:386-389`), and #7's loader refuses a profile whose `caps.global` is above `caps.open` or whose quota or job class cap is above `caps.global` (`contracts/tenancy/README.md:59`). The alternative, caps derived from pool slots, would admit far fewer jobs than the backlog that Temporal holds well | P.1, A3 |
| D2 | One admission cycle | A loop: read the row snapshot (D11), plan with P.1's policy, carry out at most 50 grants (an estimate), and run again at once when a grant moved a job, or after 1 s otherwise (an estimate). A queue grant whose pull returned nothing, or whose message was released, moved no job, so a cycle over empty queues sleeps. The scans of S.2 run every 10th cycle (an estimate) | One loop keeps the order of resumes, re-admissions and queue pulls in one place, and a cycle that admits nothing costs one snapshot a second. The alternative, a pull per message as it arrives, cannot see caps across queues | P.1, C.2, S.2, M.1 |
| D3 | Pull on credit | For a grant on a queue, admission pulls exactly one message with a drain of credit 1, and an empty queue returns at once (S4 check 2). Admission never prefetches and never holds more than its grants of the cycle unsettled. The pull is how admission learns that a queue holds work: every profile queue is offered to the policy as waiting (0.4 Policy), and a queue whose pull returned nothing is marked empty in memory and left out of `waiting` until 1 s (an estimate) after that pull, so an empty queue costs one pull a second | The `admission` user only reads the intake queues over AMQP (`contracts/broker/topology.yaml:48`), and queue depth is served only by the broker's metrics endpoint to #13 (D14), so admission has no depth to read. S4 finding 1: the stock receiver over-pulls, so the client creates it with credit 0 (`broker-client.md:47-49`), and #7's built `Consumer.pull` and `drain` grant credit only for the call and drain what is left (`packages/broker/src/wdf_broker/client.py:172-184`) | C.2, P.1 |
| D4 | What admission reads from a message | The message only names the job: admission decodes it with `wdf_compat.read_event`, as #7's rules row for #8 says, and dead-letters it `MALFORMED` with the detail token `wrong-type` for an event that is not `job.accepted`, `unknown-tenant` or `unknown-class` for a pair outside the tenancy profile, and `queue-mismatch` when its `tenant` or `priorityclass` differs from its queue. Otherwise it reads `jobid` and, for a dead letter, the identity attributes that the stage contract lists, and reads everything else from the row. The row wins when the message disagrees, and a disagreement is counted and logged | The body says admission trusts the row over the message, and the stage contract lets admission read only `id`, `tenant`, `jobid`, `priorityclass`, `jobclass`, `traceparent` and three data fields (`stages.yaml:29-32`), so `time` cannot be read from the message. The disagreement counter is `wdf_admission_row_message_mismatch_total` of D14 | C.1, C.2 |
| D5 | The first start argument | The accepted event rebuilt from the row by `wdf_events.build_event("job.accepted", source="/wdf/dispatch", ...)` with the row's identity columns, its `manifest_ref`, `batch_id`, `submission_key` and `tracestate`, and the row's `created_at` as its `time`, so it matches the stored accepted event in every field except `time` and no event is read. The row's `pending_patch_ref` is not read, because control refuses a patch for a job that never started (`13-dispatch-api.md:100`, D18 of #24). The same rebuild serves the message path and the scans. The event, `JobParams` and the search attributes are built and the event passes `to_bytes` before the claim, so a row whose content fails the schema is never claimed | The workflow reads only the identity attributes, the manifest and the batch of its first argument (`job.py:55-71,91-93`), and `WdfSubmittedAt` is the row's `created_at` (D6), so the stored event's `time` would buy only byte equality, which no goal needs, and #24's reconciler rebuilds a lost accepted event with the same `time` (`13-dispatch-api.md:89`). The alternative, selecting the stored event's `event_time` by its deterministic `event_id`, costs an `event` read per claim and a second path once the event history's 14-day retention has dropped it. Passing the message's own event needs a second path for the scans, which have no message | C.1, S.2 |
| D6 | `WdfSubmittedAt` | The row's `created_at` | It is a row read, the event `time` is not a read the stage contract allows (D4), and the harness's use of the event time (`packages/workflow/src/wdf_workflow/harness/__init__.py:128-134`) is not a stage. The alternative, adding `time` to the admission reads of the stage contract, is a contract change | C.1 |
| D7 | Errors while handling a message | An outage of Temporal (an `RPCError` of status unavailable, deadline exceeded or resource exhausted) or of the database (`psycopg.OperationalError`) releases the message uncounted, opens the breaker of D8 and ends the cycle. `MALFORMED` and `UNSUPPORTED_VERSION` from the typed errors of `read_event(message.body)` dead-letter at once with the error's `reason` as the detail token (`<token>: text`, cut to 1,024 characters, because #7's `dead_letter` raises `ValueError` above that, `client.py:91-97`), and the same typed errors raised later, by the rebuild of D5, are errors of the row and not of the message. A message whose job has no row is a counted retry, because the row may not be visible yet, and its last delivery is dead-lettered `ADMISSION_FAILED` with the token `no-row` and no event, as #7's rules row for #8 says. A message whose row is at `accepted` with no receipt (`published_at` null) is claimed, and the claim sets the receipt itself, because a delivered message proves its publish (D9), so no message is retried or dead-lettered for a missing receipt. `BrokerUnavailable` opens the broker breaker of D8, since #7's client does not reconnect, and every delivery held on the lost connection comes back with one delivery spent (S4b check 6). A `TopologyError` from `consumer(queue)` means a queue of the profile is missing, a defect of the deployment: admission skips that queue, retries it every tenth cycle, logs it and counts `wdf_admission_queue_missing_total`, and the other queues go on. Any other error is a counted `retry()`, sent at once, which the broker redelivers after 1 s times its delivery count, at most 30 s (`contracts/broker/topology.yaml:19-23`), and the last allowed delivery follows Q1, unless the internal breaker of D8 opens | An outage is not the message's fault and must not walk it toward the delivery limit (S4 finding 7: an uncounted `modified` never reaches it), and `release()` puts the message back ahead of every message never delivered, with its count unchanged (S4b check 13). The typed errors carry their `dlq_reason` (`contracts/events/README.md:114-115`) | C.2 |
| D8 | Breaker (detail issue 6) | One breaker per dependency, Temporal, the database and the broker (probed by reconnecting), and an `internal` breaker that opens when 5 distinct messages in a row (an estimate) fail with the same error class, such as a grant mistake or an unregistered search attribute after a bad deploy, so one systematic error cannot walk the backlog to the dead-letter queue. When the internal breaker opens, every message still held is released uncounted. While it is open, a cycle runs only after its backoff, reads the snapshot and makes one grant, its probe, which is handled in full: a probe that fails again is released uncounted, so a probe never walks a message toward the delivery limit, and the first probe that succeeds closes it. While a dependency breaker is open, no message is pulled, no claim is made, no start, `resume` signal or `describe` is tried, and no snapshot is read, so the cycle runs only that dependency's probe: `describe_namespace`, `SELECT 1` or a new `connect`. Every breaker backs off from 1 s doubling to 30 s (estimates) and closes on the first success. Its state is the gauge `wdf_admission_breaker_open{dependency}`. A failure at the start, after the claim, such as the unregistered search attribute, leaves the row at `admitted`, where it holds its live slot until the next delivery of its message, a replay or the stuck-row scan starts it (D9, D10), so one systematic start error holds at most the rows of the five messages that open the breaker and the row of the probe, which pulls the released message back first (S4b check 13) | Jobs wait safely in the queues and the rows while a dependency is down. A row claimed before the outage stays `admitted` and is started by the stuck-row scan of D10 once the breaker closes, however long the outage lasted, because D10 starts a row with no workflow at any age | C.2, M.1 |
| D9 | Row checks before a claim (detail issue 8) | Admission claims only a row in `accepted` whose `requested_action` is null. A row at `accepted` whose `published_at` is null is claimed all the same when admission holds a delivered message for it: under the `FOR UPDATE` lock the claim sets the receipt with `wdf_jobstate.mark_published`, which sets it only when it is null, and since the claim read it null under the lock, it is the call that set it, so it inserts `job.published` from `/wdf/admission` with the receipt time read back from the row, in the same transaction. A row whose receipt is already set gets no `job.published` from admission, so a row gets one `job.published`, from dispatch or from admission, whichever set the receipt. The scans of D10 hold no message and never set a receipt. A row in `admitted` is started with the same start arguments, because only a crash, an outage or a failed start after the claim leaves one (D18 of #6, D8), and the message is acknowledged after the start or after `WorkflowAlreadyStartedError`. A row in any later status, or in `accepted` with an outstanding action, is acknowledged and counted as skipped, and control (#24) owns what happens next. The skip reason is `started` for `running`, `ended` for a terminal status, and `outstanding-action` for `paused`, `resume-pending` or `accepted` with a `requested_action`, because only a control request leads to those. The claim is one transaction: `SELECT status, requested_action ... FOR UPDATE` of the row (allowed by the column UPDATE grant of `wdf_admission`), the check above on what it read, `transition(conn, job_id, "accepted", "admitted")` and `insert_events` of `job.admitted`, so a control request cannot commit between the check and the move, and a `Lost` answer is a skip. Dispatch marks the receipt just after the publish (A8), #24's reconciler republishes a row left at `accepted` without one, a row whose submit died before the mark or a never-published row resumed before it started (`13-dispatch-api.md` D7), and the claim sets a receipt still missing when the message arrives, so no row that admission moves lacks a receipt and the invariant `published-without-receipt` (`contracts/state/README.md:201`) never flags one. For that write `wdf_admission` holds a column UPDATE grant on `published_at` beside `wdf_dispatch` (`R__roles.sql:43`), which W.1 adds | The claim is a compare-and-set, so a control request that commits first wins (`packages/jobstate/src/wdf_jobstate/jobs.py:157-186`), and a request that commits after the claim is signalled to the workflow by #24. A delivered message proves that its publish landed, so the claim needs no receipt to admit it. The alternative, a counted retry with an `ADMISSION_FAILED` `no-receipt` dead letter after about 15 s (`contracts/broker/README.md:24`), dead-letters a message whenever the receipt comes late, after a crash between the publish and the mark, behind a backlog or under an alarm, which #24's reconciler cannot rule out | C.1, C.2, W.1 |
| D10 | Re-admission scans (decision 8) | Two scans run every tenth cycle. A row in `accepted` with a receipt (`published_at` set), `control_seq` above 0 and `requested_action` null was paused and resumed before it started, after its message was consumed, because only the row-only path through `resume-pending` back to `accepted` leaves that state (`jobs.py:139-147`), and it is re-admitted in its tenant's turn (decision 8, 0.4 Policy). This is #24's A6 rule, a row at `accepted` with a receipt, narrowed by the `control_seq` above 0 of #24's rules row for #8 (`13-dispatch-api.md:226,360`). Such a row with no receipt was never published, and #24's reconciler publishes it (D9), so its message admits it. A row in `admitted` whose `updated_at`, which the claim sets and a later control request on the row moves on, is more than 60 s old (an estimate, above the start call's timeout) is checked with `describe` of its workflow id, and started when the workflow does not exist, with no grant, because its `admitted` status already holds its slot in the live count. Its age does not stop the start: the workflow moves its row from `admitted` to `running` with `job.started` before it places any task, or to `failed` when its plan does not load (`job.py:159-179`), so a row still at `admitted` placed no task, and a start after Temporal purged an earlier workflow of it, past the namespace's 24 h retention (`deploy/temporal/README.md:34`), repeats none. A row whose workflow is closed, which only a terminate from outside the workflow's code leaves at `admitted`, is counted as `stale-admitted` for an operator and started once Temporal has purged that workflow. A scan describes at most 50 rows, oldest first (an estimate), and leaves out, inside its query and so before that limit, each job id it found a workflow for during the last hour (an estimate), which it keeps in memory because `REJECT_DUPLICATE` keeps that answer true while Temporal keeps the workflow, so rows whose workflows live on never fill a scan, the memory holds at most an hour of answers, and a closed workflow is described again. A start that fails for a reason other than an outage is counted and retried for that job with backoff from 1 minute doubling to 1 hour (estimates). A scan and a message that race for one job are safe, because the claim and the start are both idempotent | A re-admitted job's message is gone, so only the row can name it. The predicate needs no new column and no event read, which `wdf_admission` could not do beyond `event_id` and `event_time`. The alternative, reading the event history for `job.resumed` from `/wdf/control`, needs a grant on `event.type` and is a state contract change | S.2, P.1 |
| D11 | The row snapshot | One read per cycle of the live counts (status `admitted` or `running`, grouped by tenant and job class), the `paused` count by job class, the `resume-pending` count, the rows in `resume-pending` with `requested_action` `resume` in `updated_at` order, and, every tenth cycle, the two scan predicates of D10, all in one read-only `REPEATABLE READ` transaction so a row that moves between statements is counted once. No index is added. S.2 measures the snapshot and a scan cycle on 100,000 rows seeded with `wdf_stateops.bench`, and a snapshot above 100 ms (an estimate) becomes a `revisit` of #3 for a partial index on `status` | `accepted_job` has indexes only on (tenant, batch, status), on unpublished rows and on the manifest hash (`packages/jobstate/src/wdf_jobstate/migrations/0100_accepted_job.sql:30-33`), and migrations of `accepted_job` belong to the closed state epic | S.2 |
| D12 | Resume grants (detail issue 9) | A row in `resume-pending` with `requested_action` `resume` waits at the front of its priority class. When a slot of its tenant, job class and the global cap is free, admission sends `resume` with `ResumeSignal(control_seq=row.control_seq, manifest=row.pending_patch_ref)` to `workflow_id(job_id)` and counts the slot as granted. Until the row leaves `resume-pending`, admission queries the workflow's `status` at most every 30 s per job (an estimate) and sends the signal again only when the query shows a `control_seq` below the row's, because every signal adds to the workflow's history even when the workflow deduplicates it (`job.py:338-349`). At start-up, a `resume-pending` row with `requested_action` `resume` counts as granted when its `status` query shows the row's `control_seq` applied or when the query fails for a reason other than a missing workflow, so a restart never exceeds the cap. A row that leaves `resume-pending` for a terminal status frees the slot. A `resume` or a start-up query whose workflow does not exist, which only a job that never started can cause, frees the slot, is counted in `wdf_admission_skipped_total{reason="no-workflow"}` and is not sent again: admission keeps the job id and its `control_seq` in memory and leaves that row out of the waiting resumes until its `control_seq` changes, and the row-only move back to `accepted` is control's (A9) | The cap is never exceeded, as the body decides, and a lost signal is retried without a delivery column, which `wdf_admission` has no grant to write (`R__roles.sql:46-48`); the `status` query returns the applied `control_seq` (`job.py:444-452`) and adds no history. The alternative, writing `control_delivered_at`, needs a grant and is a state contract change | P.1, S.1 |
| D13 | Dead letters (detail issue 11) | A message refused as `MALFORMED` or `UNSUPPORTED_VERSION` that names a job, meaning its `tenant`, `jobid`, `priorityclass`, `jobclass` and `traceparent` attributes are present and pass the profile, and whose row exists with the message's tenant (read with `get_job`), gets `job.dead.lettered` with its `dlq_reason` and a detail of at most 1,024 characters, with source `/wdf/admission`, before the message is dead-lettered. Admission reads the stored event ids first and writes an id once, as the emit point of #6 does (D17 of #6). A message that names no job, or names a job with no row or with another tenant, is dead-lettered with no event (`stages.yaml:35-36`) | The stage contract gives the event to admission only for a message that names a job, the `event` table has no foreign key to `accepted_job` (`packages/eventstore/src/wdf_eventstore/migrations/0200_event.sql:5-19`), so the tenant check keeps one tenant's refused message out of another tenant's job history, and the row of a dead-lettered job stays `accepted` so a replay admits it | C.2 |
| D14 | Metrics (detail issue 12) | `wdf_admission_live_jobs{job_class}`, `wdf_admission_open_jobs`, `wdf_admission_paused_jobs{job_class}`, `wdf_admission_resumes_waiting{priority_class}` and `wdf_admission_breaker_open{dependency}` (gauges), `wdf_admission_admitted_total{priority_class,job_class}`, `wdf_admission_resumed_total{priority_class}`, `wdf_admission_readmitted_total{reason}` with `resumed-before-start` and `stuck-admitted`, `wdf_admission_skipped_total{reason}` with `started`, `ended`, `outstanding-action`, `no-workflow` and `stale-admitted`, `wdf_admission_start_failed_total`, `wdf_admission_queue_missing_total`, `wdf_admission_duplicates_total`, `wdf_admission_row_message_mismatch_total` and `wdf_admission_dead_lettered_total{reason}` (counters), and `wdf_admission_wait_seconds{priority_class}` and `wdf_admission_snapshot_seconds` (histograms). The names are served by the service's own Prometheus endpoint, as the workflow service serves its metrics, and not through the `wdf_events.metrics` registry, which counts only the names pinned in `metrics.yaml` (`packages/events/src/wdf_events/metrics.py:13,43-46`). No label holds a tenant, a job id or a task id. Queue depth is read from RabbitMQ by #13, not from admission. #7's client also counts `wdf_broker_publish_total`, `wdf_broker_settle_total` and `wdf_broker_dead_letter_total` in the `wdf_events.metrics` registry (instrumentation v2) inside the admission process, which #13 exports (#7's D15, `10-broker.md:122`) | The instrumentation contract allows the priority class and the job class as labels and bans unbounded identifiers (`contracts/instrumentation/README.md:19-24`), and the tenant is not on its allowed list. #13 adds the names to `metrics.yaml` | M.1 |
| D15 | Service login and configuration | One login `admission` in `wdf_admission`, its password generated on first use into the state directory and never committed, and a Secret `admission-logins` with `WDF_ADMISSION_DSN`. A second Secret `admission-broker` holds the password of #7's `admission` user, which M.2 takes from `uv run --frozen python deploy/rabbitmq/config.py password admission`, the helper that generates `<state>/rabbitmq/admission.password` on first use (A6). The service reads only the environment: `WDF_TEMPORAL_ADDRESS`, `WDF_TEMPORAL_NAMESPACE`, `WDF_ADMISSION_DSN`, `WDF_BROKER_ADDRESS` (`rabbitmq.wdf-infra.svc.cluster.local:5672`), `WDF_BROKER_PASSWORD`, `WDF_TENANCY_PROFILE` (which the module maps from `WDF_TILT_PROFILE`, as #7's tenancy contract says), `WDF_METRICS_ADDRESS`, `WDF_READY_FILE`, the optional `WDF_ADMISSION_GLOBAL_CAP`, which can only lower `caps.global` and exists so the driver can prove the cap at T2, and the optional `WDF_POOL_QUEUE_PREFIX` of Q5, which exists so the driver's jobs reach the driver's fake pools. The module sets neither optional variable. `deploy/admission/logins.py` copies the shape of `deploy/workflow/logins.py` with one login | The workflow service reads the same names (`service/__init__.py:51-68`), and a shared login helper would change the closed workflow epic's files, a `revisit`, for about 150 lines. #7 leaves the Secret and the variable names to each consumer (`deploy/rabbitmq/README.md:32-45`), so admission names its own | M.1, M.2 |
| D16 | Deployment | Module `admission` in `wdf-control` on the infra pool, one replica with strategy `Recreate`, image built like the workflow worker's by its own `deploy/admission/image.sh` and `Dockerfile` on the pinned `python` image (`image.sh` exports the hash-pinned third-party packages, fills `dists/` through `deploy/wheel-cache.sh` and builds the workspace wheels, and the `Dockerfile` installs from the `dists/` bind mount with `--network=none`, `deploy/workflow/image.sh`, `deploy/workflow/Dockerfile`), requests 50m and 128 MiB and limits 250m and 128 MiB (the CPU figures are estimates of the capacity scout), readiness through the ready file, `terminationGracePeriodSeconds` 30 so the drain of D18 finishes before a kill, and no worker versioning and no build id, because admission runs no workflow code. The `Dockerfile` carries the proton build stage of #7's D22 as S4b built it, fed by that cache: `gcc` and `libc6-dev` from `apt-get`, and `pip wheel --no-binary python-qpid-proton` with `--no-index` from the sdist that `wheel-cache.sh` put in `dists/`, because the package publishes no Linux wheel (`flake.nix:109`), in the build stage, and only the built wheel and the other packages of `dists/` in the runtime stage (`docs/delivery/spikes/S4b/broker-client-py313.md:74-78`, `docs/delivery/spikes/S4b/code/Dockerfile`), so #7's client builds from its sdist. The built wheel does not carry the sdist hash that `uv.lock` pins, so how the runtime stage keeps `--require-hashes` for the other packages is M.2's to settle. No image on `main` builds the client yet, so admission's is the first | The body's single replica with no leader election, the reserve row of the capacity revisit, and the workflow module's shape (`deploy/workflow/module.yaml`, `Tiltfile.module:91`). No YAML other than `module.yaml` and Kubernetes manifests is added under `deploy/admission/`, because `kubeconform -strict` lints every other YAML file under `deploy/` (D23 of #6) | M.2 |
| D17 | Memory | M.1 measures the peak resident memory of the service at T1 through a burst of 2,000 admissions, and pastes it. The burst runs #7's real client, with its reactor, connection and buffers, against RabbitMQ from `broker_container`, beside Postgres, which are the two containers `wdf_itest` allows (`packages/itest/src/wdf_itest/session.py:21`), so the starts go to the recording start fake of C.2, which stands in for the workflow by moving each started row from `admitted` to `running` and then to `succeeded` with `wdf_jobstate.transition` on the test's own superuser connection, so slots free under `caps.global` and no row reaches the 60 s `describe` of D10, over a Temporal client that the service's own setup connects with `lazy=True`, so the SDK's native core is loaded with no server. The Temporal connection's own buffers stay out of this figure, and exit row 11's `oom_killed: 0` covers the deployed pod. A peak above 112 MiB (an estimate that keeps a margin under the 128 MiB limit) stops M.2 with a needs-decision status, because the reserve row of the capacity revisit is 128 MiB | The 128 MiB of S8 is an estimate (`docs/delivery/spikes/S8/state-footprint.md:164`), and the Temporal client's native core, `psycopg`, `pydantic` and `python-qpid-proton` share it | M.1, M.2 |
| D18 | Drain on SIGTERM | Stop pulling, finish the grants of the cycle in progress, release every message not yet settled before closing the broker connection, and exit within 20 s (an estimate), which every Temporal call meets by an `rpc_timeout` of 10 s (an estimate, and the timeout D10's 60 s sits above). A claim committed before the stop and not yet started is started by the next process, through its message or the stuck-row scan | `Recreate` gives no overlap, and every step of a grant is idempotent. A delivery still held at `Connection.close()` counts as failed (`client.py:280-281`, S4b check 6), so the release comes first | M.1 |
| D19 | Proof of the deployed service | The T2 exit test drives the deployed admission with `python -m wdf_admission.drive` on the host, which seeds rows, accepted events and manifests and publishes intake messages and marks their receipts as the dispatch API would, makes the control moves as the control API would (the request and row-only move of a job that never started, and for a running job the `pause` request with its signal and the `resume` request that moves the row from `paused` to `resume-pending`), and serves the pool queues with `FakePool` workers behind a fresh queue prefix that it sets on the deployed admission for the run (decision 17, Q5), as the workflow epic's harness does (`08-workflow-engine.md:114`, `packages/workflow/src/wdf_workflow/harness/__init__.py:9-12`) | #24 is specified and not built, the pool workers of #10 serve only the contract queues and ran 10 jobs in 6.5 minutes (`11-workers.md:527`), and the cluster is the only place where the broker, the database, Temporal and admission meet, because T1 holds at most two containers across the host | V.1, section 7 |
| D20 | CI | No new CI workflow and no change to `.github/workflows/`. M.2 runs `scripts/tilt/run.sh ci` itself and pastes the output, and the exit test records a green `ci` run on `main` | The `state-modules` path filter does not list `deploy/admission/**`, as the workflow epic recorded for its modules (D25 of #6) | M.2, section 7 |

### 0.4 Proposals the lanes pin

No contract is added by this epic, which matches the method's epic order ("E12 #8 Admission: none new").
Admission adds no configuration file either: its caps, quotas, class order and reserve interval come from #7's tenancy contract (A3).

| Item | Proposal | Epic item |
| --- | --- | --- |
| Package | `packages/admission/` (`wdf_admission`), depending on the workspace packages `wdf-events`, `wdf-keys`, `wdf-jobstate`, `wdf-eventstore`, `wdf-stages`, `wdf-temporal`, `wdf-workflow` (for `naming()`, `ResumeSignal` and `QUEUE_PREFIX_MEMO`, and, in the driver, `FakePool` and `fresh_prefix`), `wdf-compat` (for `read_event`) and #7's `wdf-broker`, and on the locked `temporalio`, `psycopg`, `pydantic` and `hypothesis` (tests) | detail issue 14 |
| Caps | Read with `wdf_broker.tenancy.load(os.environ["WDF_TENANCY_PROFILE"])` (A3) into `wdf_admission.policy.Caps`, a frozen dataclass of `open`, `global_`, the tenant quotas, the job class caps and `reserve_interval`, each the `.value` of #7's `Estimate` (`packages/broker/src/wdf_broker/models.py:219-247`), with `global_` lowered by `WDF_ADMISSION_GLOBAL_CAP` when it is set. `load` raises `KeyError` for a profile the contract does not name | detail issues 5, 14, D1 |
| Policy | `wdf_admission.policy.plan(snapshot, caps, priority_classes, waiting) -> list[Grant]`, pure, where `priority_classes` is the ordered list of #7's tenancy profile. `snapshot` holds the live counts by tenant and job class, the open count (the live count plus the rows in `paused` and `resume-pending`), and the slots of granted resumes. `waiting` holds the resumes and the resumed-before-start re-admissions of D10 with their priority class, tenant, job class and `updated_at`, and the queues that may hold work: each queue of the profile that is neither in the empty backoff of D3 nor missing (D7), with the job class it is blocked on, if any. Intake sets a queue's blocked class when it releases a message of a class at its cap (Q4) and clears it when that queue's next pull admits a job. A `Grant` names a resume, a re-admission or a queue. The order: strict by priority class with every `reserve_interval`-th grant going to the lowest class that waits, inside a class first the resumes in `updated_at` order and then the tenants round-robin, a tenant's turn granting its oldest re-admission before its queue, and no grant past the global cap or the tenant quota, and no queue grant or re-admission past the open cap. A resume or re-admission is never granted past its job class cap, and a queue blocked on a class at its cap gets no grant. The job class of any other queue grant is known only after the pull, so the plan counts that grant against the global cap, the tenant quota and the open cap, and intake checks the pulled job's class against its cap with the cycle's earlier grants counted, releasing it as Q4 says | detail issues 5, 9, Q2 |
| Claim and start | `wdf_admission.claim.claim(conn, job_id) -> Claimed | Skipped` (D9), `wdf_admission.claim.accepted_of(row) -> Event` (D5), `wdf_admission.start.start(client, row, accepted, prefix)`, which starts with `naming().workflow.type`, `args=[accepted, JobParams(seed, parameters)]`, `id=workflow_id(job_id)`, `task_queue=naming().workflow.queue`, `id_reuse_policy=REJECT_DUPLICATE` and the five search attributes from the row (`WdfTenant`, `WdfPriorityClass`, `WdfJobClass`, `WdfJobId`, `WdfSubmittedAt` from `created_at`), the memo `{QUEUE_PREFIX_MEMO: prefix}` only when `prefix` is not empty (decision 17, Q5), and treats `WorkflowAlreadyStartedError` as a duplicate. The harness's `start` (`harness/__init__.py:594-605`) is the reference shape | detail issues 4, 10 |
| Stage | `wdf_admission.stage.admission(inbound, facts) -> list[Event]`, the function that builds `job.admitted` and `job.dead.lettered`, which `run_stage_contract("admission", admission)` checks | decision 11 |
| Intake | `wdf_admission.intake`: the queue names of the profile from `wdf_broker.topology.intake_queues(profile)`, the pull and the empty backoff of D3, the blocked class of Q4, the parse and the dead letters of D7 and D13, the settle after the start, the breaker of D8 and Q1's last-delivery rule, over #7's broker client (A1) | detail issues 4, 6, 11 |
| Scans and resumes | `wdf_admission.scan` (the snapshot of D11 and the predicates of D10) and `wdf_admission.resume` (D12) | detail issues 7, 8, 9 |
| Service | `python -m wdf_admission.service` runs the cycle of D2 in one process with the Prometheus endpoint of D14, the ready file and the drain of D18, configured by D15. Exit 2 when a variable is missing or when `WDF_POOL_QUEUE_PREFIX` puts a pool queue outside the queue name pattern of the activity contract (`packages/workflow/src/wdf_workflow/models.py:51`), and exit 1 with the loader's reason when `wdf_broker.tenancy.load` refuses the tenancy profile, both before the ready file exists | detail issues 12, 14 |
| Driver | `python -m wdf_admission.drive --kube-context NAME {smoke,control,deadletter,restart}` with `--jobs N`. It opens its own loopback port-forwards to `appdb`, `objectstore`, `temporal` and `rabbitmq`, reads the superuser password and the store identities from the state directory as the workflow harness does, seeds rows, accepted events and manifests as the dispatch API would, publishes intake messages with #7's client as the `dispatch` user and marks each receipt as the dispatch API would, makes the control moves of D19 through `wdf_jobstate` and the Temporal client as the control API would, reads the dead letters as the `operator` user, both passwords from `<state>/rabbitmq/` through #7's helper, and runs `FakePool` workers on the host behind a `fresh_prefix()` that it sets as `WDF_POOL_QUEUE_PREFIX` on the deployment for the run and removes after, waiting for each rollout and cleaning up on an abort as Q5 says. It prints `queue_prefix:`, `published:`, `finished:`, `failed:`, `max_live:` (the largest live count it sampled) and `duplicates:`, plus `skipped:`, `readmitted:` and `resumed:` for `control`, `dead_lettered:` with the reasons, `events:` and `rows: accepted N` (the dead-lettered rows still `accepted`) for `deadletter`, and `restarts:` and `oom_killed:` (the last termination reason of each deleted pod, read before the delete, and of the last pod, read before the driver restores the deployment) for `restart`. A `--global-cap N` option sets `WDF_ADMISSION_GLOBAL_CAP` on the deployment for the run and removes it after. It reuses the port-forward helpers of `wdf_workflow.harness.cluster`, `wdf_workflow.harness.fresh_prefix` and `wdf_workflow.testing.FakePool` by import and changes none of them | D19 |

### Rules agreed with epics drafted in parallel or not yet built

These are stated so the owner epic confirms them in its own spec, and section 2 lists each as an assumption.
The rows of #7, #10 and #2 record what is built, and the rest are still for their owners to confirm.

| Epic | Rule |
| --- | --- |
| #7 Broker | Built and closed on 2026-11-16 as A1 to A7 record: `wdf_broker.client`, the topology of `contracts/broker/`, `contracts/tenancy/tenancy.yaml` with `standard` as the only class, `caps.open` and the `ci` figures of D1, `reasons.yaml` with `ADMISSION_FAILED`, `FakeBroker`, `broker_contract` and `broker_container`, the RabbitMQ module with the `admission` user, and replay. Admission keeps #7's rules row for #8 (`10-broker.md:254`) in full, and confirms the address and the Secret hand-off (D15), the four release causes (an outage in D7, a class at its cap in Q4, the internal breaker in D8, the drain in D18) and the caps with `caps.open` (D1), which it reads and never changes |
| #24 Dispatch and control | Inserts the row and its accepted event in one transaction before the publish and marks the receipt after it (A8), republishes a row left at `accepted` without a receipt and sets a receipt only when it is null, so a receipt that admission's claim set stands (D9), refuses a patch for a job that never started (D5), makes the row-only moves of a job that never started in the transaction of its request, moving a resumed never-started job back to `accepted` itself, never in two transactions, and finishing that move for a `resume-pending` row with no workflow (A9, D12), signals `cancel`, `terminate` and `pause` to a job in `admitted` and retries until the workflow exists (A10), has settled the job class vocabulary with E.2, so the row always holds a class of `limits.yaml` (A11, held), and never signals `resume` |
| #10 Kubernetes workers | Built and closed on 2026-11-16: the pool workers poll the contract queues `wdf-compile`, `wdf-simulate` and `wdf-aggregate` on every `ci` cluster, and a job whose start carries the memo `wdf_pool_queue_prefix` runs its tasks on the queues behind that prefix (#10's D22, `11-workers.md:203`). Admission starts every real job with no memo, so it reaches the real pools, and only the driver's jobs carry the driver's prefix (decision 17, Q5) |
| #2 capacity revisit (#366) | Landed at `c8fa830`: the `ci` infra node is 6,144 MiB, `limits.cpu` is gone from the quotas, and `scripts/cluster/budget.sh` holds the reserve row `admission` at 50m and 128 MiB of requests, which it drops once `deploy/admission/module.yaml` exists, and `just cluster-verify` runs it (A12) |
| #13 Observability | Adds D14's names to `metrics.yaml`, owns the alerts on them (a dead letter, a breaker open for longer than 5 minutes, live jobs at the global cap and open jobs at the open cap for longer than 30 minutes, and a `stale-admitted` skip, all estimates), and decides whether `wdf_admission_paused_jobs` replaces `wdf_workflow_paused_jobs`, which the #6 rules row names but nothing emits |
| #15 Failure scenarios | Drives an admission crash between the claim and the start, and a Temporal and database outage, through the breaker of D8 and the scans of D10, with `python -m wdf_admission.drive restart` as the starting point |
| #14, #16 | Tune the caps of D1 by value PRs to #7's tenancy profiles, and #16 measures the service's real memory and CPU against D16 |
| #18 Checkpoint | A paused long job releases its slot through its row, and its resume is granted by admission (D12) |
| #17 Test ladder | The smoke job passes through the deployed admission, and the e2e core pass reuses the driver's `control` and `deadletter` journeys or their logic |

## 1. Goal and non-goals

Goal: layer 1 scheduling, job admission.
Admission decides when a submitted job may start, so that up to 100,000 queued jobs do not become 100,000 live workflows.
Jobs wait in the intake queues and the job table, not in Temporal, until admitted, and admission releases a job by starting its workflow and holds no task backlog of its own.
Once a job is admitted, admission never holds, drops or delays its tasks.

### Breakdown inputs updated for what landed

The epic body was written on 2026-10-27, before the workflow epic was specified, and these items in it are stale.

| Epic body item | Now | Source |
| --- | --- | --- |
| Start contract: workflow id from tenant and submission key, proposed attribute names | `workflow_id(job_id)`, `REJECT_DUPLICATE`, and the five pinned search attributes | decision 3 |
| The start arguments | The accepted event and the params block, with `admitted` committed before the start | decisions 3 and 4 |
| Resume released by admission, with no ordering rule | The D19 rule of #6: `resume` only while `requested_action` is `resume`, with the row's `control_seq` and pending patch | decision 3, D12 |
| Client setup, `job.admitted`, stage contract | Not in the body | decisions 11 and 12 |
| Dead-letter reasons `MALFORMED`, `INVALID_DAG`, `UNKNOWN_TENANT`, `UNSUPPORTED_VERSION` | `MALFORMED` and `UNSUPPORTED_VERSION` only, the other two dropped or moved to the API edge, and the delivery-limit case per Q1 | decision 7 |
| Live-job accounting by result watchers and visibility recovery | The job rows, per Q3 (a), because visibility counts paused workflows as live | decision 5, Q3 |
| Admission metrics: depth, running, paused, resumes waiting, admitted, rejected, deduplicated, admission wait | Every one is a D14 name except queue depth, which #13 reads from the broker's own metrics endpoint (#7's D15, `10-broker.md:122`), because the `admission` user only reads the intake queues over AMQP | D14 |
| A job paused before it started and then resumed | Not in the body; re-admitted by the scan of D10 | decision 8 |
| A row stuck at `admitted` | Not in the body; the stuck-row scan of D10, because the invariant `running-without-workflow` checks only `running` (`contracts/state/README.md:199`) | decision 8 |
| Open questions 2 (unknown tenant) and 3 (who sets the job class) | Decided | decisions 7 and 9 |
| Priority classes `high`, `normal`, `low` in diagram 10 | `standard` only | decision 6 |
| "Static configuration: tenants, quotas, class list, caps, reserve interval" | All in #7's tenancy contract, as the method's E10 row gives the cap format to #7 (`docs/delivery/method.md:174`), with the open-workflow cap `caps.open` pinned in tenancy v1 | A3, D1 |
| The database login | Not in the body; admission's own login in `wdf_admission` | decision 10 |
| Detail issues numbered 1, 4 to 15 | 2 and 3 never existed; the mapping in section 4 covers the rest | section 4 |
| The cluster | The capacity revisit (D4 of the realignment) landed at `c8fa830` with the `admission` reserve row on the infra node | decision 14 |
| Verifier `verify-dispatch` | Retired | decision 1 |

### Delivered already, and what is left

| Epic body item | State on `main` | Where it is, or what is left |
| --- | --- | --- |
| Guarded claim, row read, event insert | Delivered by #3 | `wdf_jobstate.transition`, `get_job`, `wdf_eventstore.insert_events`, the `wdf_admission` grants of `R__roles.sql:35-53` |
| Event models, builder, typed parse errors, key formulas, stage table | Delivered by #4 | `packages/events/`, `packages/keys/`, `packages/stages/` |
| Temporal naming, the workflow, the resume signal, the fake pool worker | Delivered by #6 | `contracts/temporal/`, `wdf_workflow.job`, `wdf_workflow.models.ResumeSignal`, `wdf_workflow.testing.FakePool` |
| Reference start and claim | Delivered by #6 as the harness, which claims rows and starts workflows as admission would | `harness/__init__.py:594-605`, `harness/cluster.py:269-271` |
| Broker client, topology, tenant and class configuration, RabbitMQ module, dead-letter tool | Delivered by #7 | `packages/broker/`, `contracts/broker/`, `contracts/tenancy/`, `deploy/rabbitmq/`, assumptions A1 to A7 |
| Pool workers, and the queue prefix that keeps a fake run off them | Delivered by #10, with the prefix in `wdf_workflow` | `packages/worker/`, `deploy/workers/` (modules `compile-worker`, `simulate-worker` and `aggregate-worker`), `wdf_workflow.taskpath.QUEUE_PREFIX_MEMO`, `wdf_workflow.harness.fresh_prefix`, decision 17 |
| Admission package, policy, intake loop, scans, service, module, driver, receipt from a delivered message | Left | lanes P, C, S, M, V and W |

Non-goals for this pass:

- everything the body lists under "Does not own": queue declaration, dead-letter queue and replay tooling (#7), the workflow and its versioning (#6), placement (#9), the job table and its migrations (#3), the message schema (#4), validation (#5), the API handlers and the submit sequence (#24), task retries (#6, #18), dashboards and alerts (#13)
- the layer 2 backlog gate (detail issue 15, later and not v1), pre-emption, a Kubernetes headroom model, dynamic tenant management and leader election
- the deterministic admission simulator and the load presets of detail issue 13, which #14 and #16 build from P.1's pure policy
- more than one priority class in the shipped configuration (decision 6)

## 2. Interfaces

### Consumes (each pinned to a version)

| From | What | Pinned at | Stub used until it exists |
| --- | --- | --- | --- |
| Dev environment | Dev shell, `uv`, `just test`, `just itest`, `just ci`, `just up`, `just down`, `just nuke`, `wdf_itest` | `flake.lock`, `uv.lock` (`temporalio` 1.35.0, `hypothesis` 6.170.14, `python-qpid-proton` 0.40.0, built from its sdist with the dev shell's `gcc`, `flake.nix:112`) | none, exists [read] |
| Cluster | Preset `ci`, module contract, namespaces, cluster lock | `deploy/presets/ci.yaml`, module v1, topology v1 | none, exists |
| Cluster capacity revisit (#366, a `revisit` of #2) | Infra node at 6,144 MiB, quotas without `limits.cpu`, `scripts/cluster/budget.sh` with the `admission` reserve row, and a real `just cluster-verify` | `c8fa830` (PR #371) | none, landed (decision 14) |
| Envelope | `build_event`, `wdf_compat.read_event` and the typed errors of `wdf_events`, `to_event_row`, `workflow_id`, `cloudevent_id`, `stage_span`, `tracing_interceptor`, `event_data_converter`, the stage table and `run_stage_contract`, the status table | events v4, keys v1, stages v1, status v1, instrumentation v2 (#7's three `wdf_broker_*` counters, and admission adds no name to it) | none, exists |
| Validator | `classes: [short, long]` of the `ci` limits profile | job v2 | none, exists |
| State | `transition`, `get_job`, `insert_events`, role `wdf_admission`, `wdf_stateops.bench` for D11's measurement | state v2, eventstore v1, jobstate v1 | none, exists |
| Workflow | `naming.yaml`, `ResumeSignal`, `JobWorkflow.run(accepted, params)`, `QUEUE_PREFIX_MEMO`, `FakePool`, the harness's `fresh_prefix` and port-forward helpers, the deployed workflow worker | temporal v2, workflow v1 | none, exists |
| Broker (#7) | `wdf_broker.client`, `wdf_broker.tenancy`, `wdf_broker.topology.intake_queues`, `contracts/broker/` (topology and reasons), `contracts/tenancy/`, `wdf_broker.testing` (`FakeBroker`, `broker_contract`) and `wdf_broker.testing.container.broker_container`, the RabbitMQ module, its password helper and image pin | broker v1, tenancy v1 | none, exists |
| Workers (#10) | Real pool workers on the contract queues, which the driver keeps its jobs off with a prefix (decision 17) | activity v1, worker v1 (`11-workers.md:187`) | `FakePool` behind the driver's prefix, as in the workflow epic |
| Dispatch and control (#24) | Rows, accepted events and intake messages, row-only control moves, control signals | not built (E13) | tests and the driver write rows, events and messages, and make the control moves themselves, as D19 says |

No `revisit` child issue of an earlier epic is open that blocks this epic.
The revisits #366, #380 and #374 are closed, and D11 may add a `revisit` of #3.
The names below were checked free at `331e013` [read: no `packages/admission/`, no `deploy/admission/`, and `docs/architecture/17-admission-cycle.drawio` is this spec's own diagram].

### Assumptions about other epics

The broker (#7) and the workers (#10) were built and closed on 2026-11-16, and the capacity revisit #366 landed at `c8fa830`, so A1 to A7 and A12 to A15 below record what holds on `main` at `331e013`, and the spec review found them still holding at `2956cf1`.
The dispatch API (#24) is specified and not built, so A8 to A10 stay assumptions until its spec is approved, and the review checks each against `13-dispatch-api.md`; A11 holds on `main`.

| # | Epic | Assumption | If it does not hold |
| --- | --- | --- | --- |
| A1 | #7 | Held: `wdf_broker.client` has `connect(address, Credentials(user, password)) -> Connection`, `Connection.consumer(queue) -> Consumer`, which raises `TopologyError` for a missing queue, `Consumer.pull(n, timeout_s)` and `Consumer.drain(n, timeout_s)` over a receiver at credit 0, and a `Delivery` with `body`, `queue`, `delivery_count`, `annotations` and `last` (true at a delivery count of 5), settled by `ack()`, `retry()` (counted), `dead_letter(reason, detail)` and `release()` (uncounted) (`packages/broker/src/wdf_broker/client.py:53-109,172-184,270-281`). The calls block, and admission runs them in a thread. Errors are `BrokerUnavailable`, with no automatic reconnect, `TopologyError` and `PublishTimeout` of `wdf_broker.models`, and `Connection.close()` counts every held delivery as failed | none |
| A2 | #7 | Held: the intake exchange, one quorum queue per tenant and priority class named by the template, the delivery limit set by the policy `wdf-intake` and not by a declare argument, the delayed retry of decision 18, and the dead-letter exchange and queue, all as `contracts/broker/topology.yaml:7-25` pins them, and the queue names of a profile from `wdf_broker.topology.intake_queues(profile)` | Q4 option (b) changes the template by a broker contract change; admission builds queue names only from `intake_queues` |
| A3 | #7 | Held: `contracts/tenancy/tenancy.yaml` (tenancy v1), read through package data by `wdf_broker.tenancy.load(profile)`, which raises `KeyError` for a profile it does not name, with `priority_classes` (`[standard]` in `ci`), `reserve_interval`, `caps.open`, `caps.global`, `caps.job_class` and `tenants.<t>.quota`, each figure an estimate with a `.value`, and the `ci` figures of D1 (`tenancy.yaml:8-18`). The profile is chosen by `WDF_TENANCY_PROFILE`, which a module maps from `WDF_TILT_PROFILE` through `tilt_profiles` (`ci` for the `minimal` and `ci` Tilt profiles) | none |
| A4 | #7 | Held: `contracts/broker/reasons.yaml` lists the application reasons that admission sets, `ADMISSION_FAILED` among them with no event and recovery by replay (`reasons.yaml:20-26`), and the broker's own reasons, as decision 7 decided. The detail format and its length limit are the contract's, and `dead_letter` enforces the limit by raising `ValueError`. The broker spaces counted retries itself by the delayed retry of decision 18, so #7's D6 asks nothing more of admission | none |
| A5 | #7 | Held: `wdf_broker.testing` exports `FakeBroker(profile)`, whose `connect()` returns a connection of the same API, and `broker_contract(connect, profile, timeout_s, checks)`, which #7 runs against the fake and against the real client in a container, and `wdf_broker.testing.container.broker_container(t1, profile)` starts the pinned broker for a T1 test (`packages/broker/src/wdf_broker/testing/__init__.py`, `testing/container.py`) | none, and lane 0 writes no fake of its own (decision 20) |
| A6 | #7 | Held: `images.rabbitmq` 4.4.0 by digest (`deploy/versions.yaml:38-41`), module `rabbitmq` in `wdf-infra` for the `dev`, `full` and `ci` profiles, reachable at `rabbitmq.wdf-infra.svc.cluster.local:5672`, and a user `admission` that only reads the intake queues (`topology.yaml:48`). Its password comes from `uv run --frozen python deploy/rabbitmq/config.py password admission`, which generates `<state>/rabbitmq/admission.password` on first use, and each consumer module makes its own Secret in its own namespace (`deploy/rabbitmq/README.md:32-45`) | none |
| A7 | #7 | Held: `python -m wdf_broker.dlq replay` republishes each selected dead letter to the intake queue it first died on, upgraded by `upgrade_for_replay`, and refuses a reason whose recovery is `never` without `--force` (`packages/broker/src/wdf_broker/dlq.py:106-124`). It never reads the job row and has no `--dsn` option, so a replayed job whose row is still `accepted` is admitted again, and one whose row moved on is acknowledged and skipped by D9 (S4 check 5) | none for admission |
| A8 | #24 | The submit sequence inserts the row and one `job.accepted` with sequence 0 in one transaction, then publishes, then calls `mark_published` (`13-dispatch-api.md:87,90`). The source `/wdf/dispatch` is the stage contract's (`contracts/stages/stages.yaml`), and #24 does not state that it builds the event from the row's columns, which admission does not need, because D5 rebuilds from the row with `time` from `created_at` | a submit that marks the receipt late only makes admission's claim set it (D9); D5 starts from the row's facts in any case |
| A9 | #24 | A cancel, pause or terminate of a job in `accepted` makes the request and its row-only move in one transaction, and the resume of a job that never started moves it from `paused` through `resume-pending` back to `accepted` itself, writing `job.resumed` from `/wdf/control` (`contracts/status/status.yaml:38-41,72`) | a split request can leave a row in `accepted` with an action and no message, which no scan of D10 admits, so this is a rule #24 must keep and not a preference; D9's row lock keeps the claim itself safe in either order |
| A10 | #24 | `cancel`, `terminate` and `pause` reach a job in `admitted` by signal, retried until its workflow exists, because admission commits `admitted` before the start | none for admission |
| A11 | #24 | Held at `2956cf1`: the row's `job_class` is checked against `short` and `long` (`packages/jobstate/src/wdf_jobstate/migrations/R__job_vocab.sql:60-62`), and E.2 of #24 settled the vocabulary of #216 on the events, which are version 4 (`7959b5c`, `contracts/events/VERSION`), so the row always holds a class of `limits.yaml` | admission reads the class from the row in any case (D4) |
| A12 | #366 (#2 revisit) | Held at `c8fa830`: the infra node is 6,144 MiB (`deploy/presets/ci.yaml:15-18`), the quotas have no `limits.cpu`, `scripts/cluster/budget.sh` holds the reserve row `admission` at 50m and 128 MiB that it drops once a `module.yaml` named `admission` exists (`budget.sh:35-39`), and `just cluster-verify` prints an `ok budget` and an `ok ready` line (`scripts/cluster-verify.sh:74-76,154-166`) | none |
| A13 | #10 | Held: the pool workers serve the contract queues on every `ci` cluster, and a job whose start carries the memo `wdf_pool_queue_prefix` runs its tasks behind that prefix (decision 17). The exit test keeps its jobs on `FakePool`s behind the driver's prefix (Q5), so admission does not depend on the real pools' speed | none |
| A14 | #24 | Held: the dispatch spec names its diagram `NN`, the next free number when its 0.2 (issue) draws it (`13-dispatch-api.md:5`), so 17 stays this spec's admission cycle diagram | none |
| A15 | #7 | Held: #7's 0.2 (issue) refined diagram 10 for `standard`, the claim from `accepted` to `admitted` and the dead-letter reasons (`10-broker.md:682`, exit row 4) | none |

The rules for #13, #14, #15, #16, #17 and #18 in section 0 are assumptions of the same kind, about specs not yet drafted, and the review checks them the same way.

### Produces (each pinned precisely)

| To | What | Where it lives (path) | Shape | Version |
| --- | --- | --- | --- | --- |
| #24, #15, #16, #17, #18 | Admission service and module | `packages/admission/` (`wdf_admission`), `deploy/admission/` | `python -m wdf_admission.service`, module `admission` in `wdf-control` (module v1) | admission v1 |
| #6's workflow, every reader of the event history | `job.admitted`, `job.dead.lettered` and, from W.1, `job.published` events from `/wdf/admission` | the `event` table | events v4 unchanged, and stages v2 from W.1 | none new |
| #13 | Admission metrics | the service's metrics endpoint | D14's names, under the instrumentation contract | admission v1 |
| #14, #16 | Pure policy | `wdf_admission.policy` | 0.4 | admission v1 |
| #15, #16, #17 | Driver | `wdf_admission.drive` | `smoke`, `control`, `deadletter`, `restart` | admission v1 |

## 3. Lanes

Package and module names are proposals.
Lane 0 owns every file it creates until the owning lane takes over the glob.

Departures from the method, stated so the review approves them knowingly:

- Lane 0 carries no `just/*.just` module and adds no recipe: tests run through `just test`, `just itest` and `just ci`, and the service and the driver are `python -m` commands, as the toolchain and workflow epics did.
- No contract is pinned in lane 0, because the epic order gives #8 none (section 0.4).
- Lane Needs run across lanes where a test over a stub would prove only the stub: the service runs the policy, the intake loop and the scans (M.1 after P.1, C.2 and S.2), the module needs the service (M.2), and the driver drives the deployed module (V.1 after M.2).
- Lane W was added by #24's spec review of 2026-11-18, after lane C's issues merged, so it takes over `claim.py`, `stage.py` and their tests from lane C, and it changes the stage contract of #4 and the state contract and grants of #3 through a contract-change PR, as E.3 of #24 changes the state contract.

| Lane | Workers | Owns (exclusive) | Issues | Needs |
| --- | --- | --- | --- | --- |
| 0 Skeleton | 1 | `packages/admission/{pyproject.toml,README.md}`, `packages/admission/src/wdf_admission/__init__.py`, `packages/admission/tests/{conftest,test_wheel}.py`, the two `wdf-admission` lines of the root `pyproject.toml`, the `uv.lock` workspace entry, `docs/architecture/10-intake-and-admission.drawio` and its svg, which #7's 0.2 (issue) has refined (A15) | 0.1 | nothing (barrier, merged alone) |
| P Policy | 1 | `packages/admission/src/wdf_admission/policy.py`, `packages/admission/tests/test_policy.py` | P.1 | lane 0 |
| C Claim, start and intake | 1 | `packages/admission/src/wdf_admission/{claim,start,stage,intake,breaker}.py`, `packages/admission/tests/core/**` | C.1, C.2 | lane 0, C.1 for C.2 |
| S Scans and resumes | 1 | `packages/admission/src/wdf_admission/{scan,resume}.py`, `packages/admission/tests/scan/**` | S.1, S.2 | C.1 |
| M Service and module | 1 | `packages/admission/src/wdf_admission/service/**`, `packages/admission/tests/service/**`, `deploy/admission/**` (module `admission`, `Tiltfile.module`, `Dockerfile`, `image.sh`, `logins.py`, `logins.sh`, render and login tests, `README.md`), the `deploy/admission` entry of `TREES` in `packages/migrate/tests/test_credentials.py`, the `admission` entry of the reserve loop in `scripts/cluster/test_budget.py` (decision 21) | M.1, M.2 | P.1, C.2 and S.2 for M.1, M.1 for M.2 |
| V Driver | 1 | `packages/admission/src/wdf_admission/drive/**`, `packages/admission/tests/drive/**` | V.1 | M.2 |
| W Receipt from a delivered message | 1 | `contracts/stages/{stages.yaml,README.md,VERSION}` and each vector under `contracts/stages/vectors/` that copies the table, `contracts/state/{README.md,VERSION}` (shared with E.3 of #24), the `published_at` grant line of `packages/stateops/src/wdf_stateops/migrations/R__roles.sql`, the admission test of `packages/stateops/tests/test_grants.py`, and, taken over from lane C, `packages/admission/src/wdf_admission/{claim,stage}.py` and `packages/admission/tests/core/test_{claim,intake,stage}.py` | W.1 | C.1, C.2 and S.2, all merged |

Shared-file rules:

- `uv.lock` is the one hotspot, and 0.1 (issue) commits it with the workspace entry of `wdf-admission`.
  Lane 0 declares in `packages/admission/pyproject.toml` every dependency the lanes need, #7's `wdf-broker` included, so no later lane edits it.
  A lock conflict is never resolved by hand, and `uv lock` is rerun after rebase.
- The root `pyproject.toml` changes only in 0.1 (issue), by one `wdf-admission` line in `[tool.uv.sources]` and one in the dev dependency group.
- `packages/admission/src/wdf_admission/__init__.py` and `packages/admission/tests/conftest.py` are lane 0's, and a lane imports by module path and keeps its own fixtures in a `conftest.py` inside its own test directory.
- A test that needs a container carries `integration`, and no pytest session holds more than two containers, because `wdf_itest` takes one of two host-wide lock files per container (`packages/itest/src/wdf_itest/session.py:21,48-50`), so two T1 workers running at once queue for the locks: C.1 and S use Postgres and the dev server, C.2 uses RabbitMQ and Postgres with a recording fake for the start, and M.1 uses Postgres and the dev server with `FakeBroker`, except its memory test, which uses Postgres and RabbitMQ through `broker_container` with the recording fake for the start (D17).
- `packages/migrate/tests/test_credentials.py` changes only in M.2, by adding `deploy/admission` to `TREES`.
- `contracts/state/{README.md,VERSION}` change in W.1 and in E.3 of #24 (`13-dispatch-api.md` section 3), so whichever lands second rebases onto the first and takes the next state version.
- `scripts/cluster/test_budget.py` changes only in M.2, by moving `admission` out of the reserve loop of `test_ci_infra_node_figures` (decision 21).
- No file under `deploy/admission/` other than `module.yaml` and Kubernetes manifests is YAML (D16).
- `justfile`, `just/*.just`, `.github/**`, `nix/**`, `scripts/**` other than the line of decision 21, `docs/dev-setup.md`, `deploy/host/ports.yaml`, `deploy/versions.yaml`, `contracts/**` other than lane W's files, every other package except lane W's two lines of `packages/stateops`, and every other `deploy/` directory are not touched.
  Admission needs no host port: the driver reaches the cluster through `kubectl port-forward` bound to 127.0.0.1.
- No tracked file holds a credential: the login password comes from the state directory, and the broker password from #7's helper (A6).

## 4. Issues

One block per issue, in the [lane issue template](../../../.github/ISSUE_TEMPLATE/lane-issue.md), replaced by `#NNN` links after filing.
Done means, for every issue: `just ci` is green on the branch, the output of the named tests is pasted in the PR, and nothing outside Owns and the shared files changed.
An issue that names a T2 command runs it under the cluster lock and pastes its output.

### Mapping from the epic's detail issues

| Epic detail issue | Delivered by |
| --- | --- |
| 1 Intake event contract | nothing new: the accepted event is events v4 of #4, and admission's reads are stages v1 (D4); C.2 parses it |
| 4 Controller core loop | C.1 (claim and start), C.2 (pull on credit, settle after start) |
| 5 Policy engine | P.1 |
| 6 Temporal health breaker | C.2 (D8) |
| 7 Live-job accounting and recovery | S.2, from the rows (Q3) |
| 8 Control API interplay | C.1 (D9), S.2 (D10) |
| 9 Pause and resume slot accounting | P.1, S.1 (D12) |
| 10 Start dedup behaviour | C.1 |
| 11 Trust-boundary dead-lettering | C.2 (D13, Q1) |
| 12 Admission metrics and traces | C.1 (the span), M.1 (D14), the catalogue entries to #13 |
| 13 Admission simulator and load scenarios | P.1's pure policy, with the presets moved to #14 and #16 |
| 14 Config and deployment | M.1, M.2, with the caps in #7's tenancy contract (A3) |
| 15 Layer 2 backlog gate | not in this pass, as the body says |

### 0.1 Package skeleton

Lane 0, tier T0. Needs: nothing. Blocks: every other issue.

- Scope: the package, the wheel test, and the admission part of the intake diagram.
  #7's 0.2 (issue) refined diagram 10 (`standard` only, the claim, the dead-letter reasons), and this issue adds only the accounting from the rows, the commit before the start and the two scans (A15).
- Owns: lane 0's globs.
- Consumes / produces: #7's `wdf_broker` package.
  Produces the package that every lane builds in.
- Acceptance criteria:
  - [ ] `pytest packages/admission -v -k wheel` builds the wheel and imports `wdf_admission` outside the repository.
  - [ ] `scripts/export-diagrams.sh --check` exits 0, and diagram 10 shows `standard` as the only class, accounting from the rows, the commit of `admitted` before the start and the two scans.
- Verifier: `packages/admission/tests/test_wheel.py`.

### P.1 Admission policy

Lane P, tier T0. Needs: lane 0. Blocks: M.1.

- Scope: `wdf_admission.policy.plan` of 0.4, pure, with the order of Q2 and the caps of D1.
- Owns: lane P's globs.
- Consumes / produces: the shape of #7's tenancy profile (A3).
  Produces the policy that M.1 runs and that #14 and #16 may drive.
- Acceptance criteria:
  - [ ] `pytest packages/admission -v -k policy` passes, including hypothesis properties over random snapshots: no plan takes the live count past the global cap or a tenant quota, and no resume or re-admission takes it past a job class cap; no queue grant or re-admission takes the open count past the open cap, while resumes are still granted; a resume is granted before any re-admission or queue of its class, and a tenant's re-admission before that tenant's queue; with three classes waiting, every `reserve_interval`-th grant goes to the lowest waiting class and otherwise the highest waiting class wins; within a class, tenants with waiting work are served round-robin, a re-admission taking its tenant's turn; a queue blocked on a class at its cap gets no grant, and one blocked on a class with a free slot does; a queue left out of `waiting` gets no grant; and a job class at its cap blocks only the resumes, re-admissions and blocked queues of that class.
  - [ ] `test_flooding_tenant_cannot_starve_quiet_one` passes: one tenant with 10,000 waiting and one with 10 both get grants within the first 20.
  - [ ] `test_readmissions_cannot_jump_tenants` passes: one tenant with 1,000 re-admissions and one with a waiting queue both get grants within the first 4.
  - [ ] `test_single_class_is_round_robin` passes with the `ci` tenancy profile and `standard` only.
- Verifier: `packages/admission/tests/test_policy.py`.

### C.1 Claim and start

Lane C, tier T1 (Postgres and the dev server through `wdf_itest`). Needs: lane 0. Blocks: C.2, S.1, S.2.

- Scope: the row checks and claim of D9 with `job.admitted` in the same transaction, the rebuild of D5, the start of decision 3 inside the span of decision 12, `WorkflowAlreadyStartedError` as a duplicate, and the stage function of decision 11.
- Owns: `claim.py`, `start.py`, `stage.py` and their tests under `packages/admission/tests/core/`.
- Consumes / produces: state v2, events v4, stages v1, temporal v2.
  Produces `claim`, `accepted_of`, `start` and `stage.admission`.
- Acceptance criteria:
  - [ ] `pytest packages/admission -v -k "claim_commits_before_start or claim_writes_admitted_event or claim_lost_is_skip or claim_skips_outstanding_action or claim_refuses_no_receipt or claim_locks_row or bad_row_never_claimed or admitted_row_is_started or already_started_is_duplicate"` passes, the first one showing that the row reads `admitted` with its `job.admitted` event before the dev server has the workflow.
  - [ ] `test_rebuilt_accepted_matches_stored` passes: the rebuild from a row seeded with `build_event` and its stored event equals the stored event in every field except `time`, which is the row's `created_at`.
  - [ ] `test_start_options_match_naming` passes: the started workflow on the dev server has type `JobWorkflow`, queue `wdf-workflow`, id `job-<job id>`, the five search attributes with their types, the two arguments and no memo, and the memo `wdf_pool_queue_prefix` with the prefix when `start` is given one (Q5), and fails when one differs from `naming.yaml`.
  - [ ] `test_stage_contract_admission` passes `run_stage_contract("admission", stage.admission)`.
  - [ ] `test_start_carries_trace` passes: the start request's header carries the row's trace id.
- Verifier: `packages/admission/tests/core/test_claim.py`, `test_start.py`, `test_stage.py`.

### C.2 Intake loop, dead letters and breaker

Lane C, tier T1 (RabbitMQ through `wdf_broker.testing.container.broker_container` and Postgres through `wdf_itest`, with a recording fake for the start). Needs: C.1. Blocks: M.1.

- Scope: the pull and the empty backoff of D3, the error rules of D7, the dead letters of D13, Q1's last-delivery rule, the breaker of D8, the head-of-line rule of Q4 and the settle after the start.
- Owns: `intake.py`, `breaker.py` and their tests under `packages/admission/tests/core/`.
- Consumes / produces: #7's client (A1), topology (A2), tenancy (A3), reasons (A4) and testing module (A5).
  Produces `wdf_admission.intake`.
- Acceptance criteria:
  - [ ] `pytest packages/admission -v -k "ack_after_start or one_message_per_grant or malformed_dead_lettered or unsupported_major_dead_lettered or dead_letter_event_once or nameless_message_no_event or outage_releases_uncounted or last_delivery_admission_failed or wrong_type_dead_lettered or unknown_pair_dead_lettered or queue_mismatch_dead_lettered or no_row_retried_then_admission_failed or no_receipt_retried_then_admission_failed or other_tenant_no_event or systematic_error_opens_breaker or internal_breaker_probes_one_grant or failure_after_claim_left_admitted or broker_breaker_reconnects or missing_queue_skipped or empty_queue_backed_off or class_at_cap_released or class_cap_counts_cycle_grants"` passes against RabbitMQ, each test reading the queue and DLQ counts by polling (S4 finding 8).
  - [ ] `test_dead_lettered_row_stays_accepted` passes, and a replayed body of that message is claimed and started.
  - [ ] `test_breaker_opens_and_closes` passes with the recording fake raising `RPCError` unavailable: no pull, signal, `describe` or snapshot while open, a probe with backoff, and pulls again after the first good probe.
  - [ ] `test_db_breaker_opens_and_closes` passes the same checks with the connection raising `psycopg.OperationalError` and the `SELECT 1` probe.
  - [ ] `class_at_cap_released` asserts that the message is released uncounted, with its `delivery_count` unchanged, that the queue is reported blocked on that class, and that the next pull of that queue returns it ahead of the messages never delivered (S4b check 13), and `class_cap_counts_cycle_grants` asserts that of two jobs of one class pulled in one cycle with one slot of that class free, one is admitted and the other released.
  - [ ] `internal_breaker_probes_one_grant` asserts that while the internal breaker is open one grant is made per backoff and a failed probe is released uncounted; `failure_after_claim_left_admitted` asserts that a start failure leaves the row at `admitted` and that the next delivery starts it; `empty_queue_backed_off` asserts that an empty queue is pulled at most once a second and that a cycle over empty queues sleeps (D2, D3).
- Verifier: `packages/admission/tests/core/test_intake.py`, `test_breaker.py`.

### S.1 Resume grants

Lane S, tier T1 (Postgres and the dev server). Needs: C.1. Blocks: S.2.

- Scope: D12, with the slot held in memory from the grant until the row leaves `resume-pending`.
- Owns: `resume.py` and its tests under `packages/admission/tests/scan/`.
- Consumes / produces: `ResumeSignal`, `JobWorkflow` with `FakePool` and `FakeControl` on the dev server.
  Produces `wdf_admission.resume`.
- Acceptance criteria:
  - [ ] `pytest packages/admission -v -k "resume_sent_with_row_seq or resume_carries_patch or resume_resent_until_running or no_resume_without_request or resume_waits_for_free_slot or terminal_frees_resume_slot or resend_only_when_unapplied or restart_counts_applied_resumes or resume_without_workflow_left_to_control"` passes, the first two reading the signal the paused workflow received through its `status` query (`control_seq` and the resumed task states).
  - [ ] `test_resume_and_cancel_same_seq_cancel_wins` passes: a cancel requested after the resume is not hidden by a stale resume, as D19 of #6 requires.
- Verifier: `packages/admission/tests/scan/test_resume.py`.

### S.2 Snapshot and re-admission scans

Lane S, tier T1 (Postgres and the dev server). Needs: S.1. Blocks: M.1.

- Scope: the snapshot of D11 and the two scans of D10, with the counts that the policy takes.
- Owns: `scan.py` and its tests under `packages/admission/tests/scan/`.
- Consumes / produces: state v2 and `wdf_stateops.bench`.
  Produces `wdf_admission.scan`.
- Acceptance criteria:
  - [ ] `pytest packages/admission -v -k "live_count_from_rows or paused_row_frees_slot or resumed_before_start_readmitted or resumed_before_start_without_receipt_left or stuck_admitted_started or stuck_admitted_with_workflow_left or old_stuck_admitted_started or closed_workflow_counted_stale or describe_capped_per_scan or describe_skips_remembered_rows or scan_and_message_race_once"` passes, `old_stuck_admitted_started` showing that a row at `admitted` with no workflow and an `updated_at` older than the namespace retention is started, and `describe_skips_remembered_rows` that 50 remembered rows older than a row with no workflow do not keep it out of a scan.
  - [ ] `test_snapshot_on_100k_rows` prints the snapshot time over 100,000 rows seeded with `wdf_stateops.bench`, pasted in the PR, and the D11 threshold decides whether a `revisit` of #3 is filed.
- Verifier: `packages/admission/tests/scan/test_scan.py`.

### M.1 Admission service

Lane M, tier T1 (Postgres and the dev server, with `FakeBroker`, and for the memory test Postgres and RabbitMQ through `broker_container`, D17). Needs: P.1, C.2, S.2. Blocks: M.2.

- Scope: the service of 0.4: the cycle of D2, the metrics of D14, the configuration of D15, the ready file and the drain of D18, and the memory measurement of D17.
- Owns: `service/**` and `packages/admission/tests/service/**`.
- Consumes / produces: every module of lanes P, C and S.
  Produces `python -m wdf_admission.service`.
- Acceptance criteria:
  - [ ] `pytest packages/admission -v -k "service_caps_hold or service_restart_recovers or service_drain_releases or service_refuses_bad_profile or service_missing_env_exit_2 or service_bad_prefix_exit_2 or metrics_named"` passes, the first one admitting 200 jobs with a global cap of 5 while sampling the live count from the rows, which never exceeds 5, and all 200 jobs finishing on `FakePool`.
    `service_restart_recovers` restarts the service with resumes granted and not yet `running`, and asserts that the sampled live count never passes the cap (D12).
  - [ ] `test_service_peak_memory` admits 2,000 jobs through the service, with #7's real client against `broker_container` and the recording start fake, which moves each started row on to `succeeded`, over a lazily connected Temporal client (D17), and prints the peak resident memory, pasted in the PR, and D17 decides whether M.2 may start.
- Verifier: `packages/admission/tests/service/test_service.py`.

### M.2 Module, image and login

Lane M, tier T2. Needs: M.1. Blocks: V.1.

- Scope: module `admission` of D16 with its image, the login and the broker Secret of D15, the `TREES` entry, and the reserve line of decision 21.
- Owns: lane M's `deploy/admission/**` globs, the `TREES` line and the reserve line of `scripts/cluster/test_budget.py`.
- Consumes / produces: module v1, the budget of the capacity revisit (A12), #7's module and password helper (A6).
  Produces module `admission`.
- Acceptance criteria:
  - [ ] `pytest deploy/admission -v` passes the render tests (one replica, `Recreate`, the resources of D16, the ready probe, `WDF_TENANCY_PROFILE` from `WDF_TILT_PROFILE`, the proton build stage, `WDF_BROKER_ADDRESS` at `rabbitmq.wdf-infra.svc.cluster.local:5672`, the Secrets by reference with `admission-broker` made from #7's helper, and neither `WDF_POOL_QUEUE_PREFIX` nor `WDF_ADMISSION_GLOBAL_CAP` set) and the login tests (one login in `wdf_admission`, apply twice changes nothing, `check` exits 1 on a changed password).
  - [ ] `pytest scripts/cluster/test_budget.py -v` passes with `admission` counted as a module and no longer as a reserve (decision 21).
  - [ ] T2: `scripts/tilt/run.sh ci` exits 0 with `admission` ready, `kubectl --context kind-wdf -n wdf-control logs deploy/admission` shows the service polling its queues, and `just cluster-verify` exits 0 with the `ok budget` and `ok ready` lines covering `admission`.
- Verifier: `deploy/admission/test_render.py`, `deploy/admission/test_logins.py`, the T2 commands.

### V.1 Driver

Lane V, tier T2. Needs: M.2. Blocks: the exit test.

- Scope: `python -m wdf_admission.drive` of 0.4 with its four journeys.
- Owns: lane V's globs.
- Consumes / produces: the deployed admission, `FakePool`, the harness's `fresh_prefix` and port-forward helpers.
  Produces the driver that the exit test, #15, #16 and #17 run.
- Acceptance criteria:
  - [ ] `pytest packages/admission -v -k drive` passes the driver's own T0 tests (argument parsing, counters, the refusal of a cluster it cannot reach with exit 2).
  - [ ] T2: rows 8 to 11 of section 7 pass and their output is pasted.
- Verifier: `packages/admission/tests/drive/test_drive.py`, the T2 commands.

### W.1 Receipt from a delivered message

Lane W, tier T1 (Postgres and the dev server through `wdf_itest`). Needs: C.1, C.2 and S.2, all merged. Blocks: the exit test.

- Scope: the claim of D9 for a row with no receipt, in two PRs.
  First a contract-change PR, labelled `contract-change`, touching `contracts/` only, with a comment listing the consumers of the stage and state contracts.
  The stage contract gives `job.published` to admission with source `/wdf/admission` and the `when` "the claim set a missing receipt from a delivered message", gives the dispatch entry the `when` "the dispatch stage set the receipt after the broker accepted its publish", updates the README's stage table and its note on types two stages emit, and bumps `VERSION` to 2.
  The state contract's writers table adds the receipt write to admission's row, and its `VERSION` takes the next number.
  The status table needs no change, because `job.published` sets no status from either source (`contracts/status/README.md:36`).
  Then the code PR: `GRANT UPDATE (published_at) ON accepted_job TO wdf_admission` in `R__roles.sql`, the claim of D9 in `claim.py`, `RETRIED` left with `no-row` alone, and `stage.admission` building `job.published`.
  `intake.py` is unchanged, because it reads `RETRIED`.
- Owns: lane W's globs.
- Consumes / produces: state v2, stages v1.
  Produces stages v2, the next state version, and `job.published` from `/wdf/admission`.
- Acceptance criteria:
  - [ ] `contracts/stages/check.sh contracts/stages/stages.yaml` exits 0, and `pytest packages/stages -q -rs` passes with no `SKIPPED` line.
  - [ ] `pytest packages/stateops -v -k admission_role` passes against Postgres: `wdf_admission` sets `published_at` and is still refused every column it could not write before.
  - [ ] `pytest packages/admission -v -k "claim_sets_a_missing_receipt_once or claim_with_receipt_records_no_published or claim_and_mark_race_records_one_published or stage_contract_admission"` passes: a claim from a message whose row has no receipt sets it, admits the row and records one `job.published` from `/wdf/admission` with the receipt time; a claim of a row whose receipt is set records none; and dispatch's `mark_published`, as #24's reconciler calls it, racing the claim of a message for one row leaves one receipt and one `job.published`, from whichever set it.
    `test_claim_refuses_no_receipt` becomes the first of these, and `test_no_receipt_retried_then_admission_failed` is removed with the `no-receipt` dead letter.
  - [ ] `pytest packages/admission -q -rs` passes with no `SKIPPED` line.
- Verifier: `packages/admission/tests/core/test_claim.py`, `test_stage.py`, `packages/stateops/tests/test_grants.py`.

## 5. Order, stubs and spikes

- First: 0.1 (issue), alone, with an estimate of 1.5 days.
- After lane 0: P.1 and C.1 in parallel.
- After C.1: C.2 and S.1, then S.2 after S.1.
- After P.1, C.2 and S.2: M.1.
- After M.1: M.2, then V.1, then the exit test.
- After C.1, C.2 and S.2: W.1, which was added after V.1 merged, so it runs after V.1 and before the exit test.
- DAG: 0.1, then (P.1 ‖ C.1), then (C.2 ‖ S.1 then S.2), then M.1, then M.2, then V.1, then W.1, then the exit test.
- Cluster work: M.2, V.1 and the exit test, each under the cluster lock, one at a time.

Stubs:

| Stub | What it fakes | How its conformance test runs against the real thing |
| --- | --- | --- |
| `FakeBroker` of #7 (A5) | #7's client and RabbitMQ | #7 runs `broker_contract` against the fake and against its client on a RabbitMQ container (`10-broker.md:528`), and C.2 and M.1's memory test (D17) run the real client against `broker_container` |
| Recording start fake in C.2 tests and M.1's memory test | the real start of C.1 | C.1's tests run the real start on the dev server, and M.1 joins both |
| `FakePool` behind the driver's prefix | the pool workers of #10 | #10 runs `activity_contract` against each real pool worker behind a prefix (`11-workers.md:380`), as the workflow epic set up |
| Rows, events, messages and control moves written by tests and the driver | the dispatch and control API of #24 | #24's submit and control tests check the shapes of A8 to A10, and the smoke gate of #17 runs the real chain |

Spikes: none.
The broker questions were answered by S4, and the one open point it left for admission, where a released message lands in a quorum queue, was settled by #7's S4b check 13 (decision 18), which C.2's `class_at_cap_released` test asserts.

## 6. Test delta

Tests: each issue in section 4 names its tests and tier, T0 for the policy and the driver's own tests, T1 for the claim, the intake loop, the scans, the resumes and the service under `packages/admission/tests/`, and the render tests at T0 and the login tests at T1 under `deploy/admission/`, all run by `just test`, `just itest` and `just ci`.
Commands: `python -m wdf_admission.service`, `python -m wdf_admission.drive` with its four journeys at T2, and `deploy/admission/image.sh`.
No helper script, verifier skill, feature file or verify recipe (decision 1).
What stays untested here is listed under "Not provable by this epic" in section 7, and the exit test proves the epic from a clean export.

## 7. Exit test

Run by a worker who did not write the lanes, on a clean `git archive` export of `main` taken outside any git repository, on the 20 GiB WSL2 host with Docker running for the whole run.
Containers other than the ones the run starts are never touched.
Each command runs from the export root, and its real output is the evidence file `exit-test/row-<#>.txt` beside the export, pasted in the PR that records the run and summarised in Closure.
Every command takes `nix develop -c`, and every Python command takes `uv run --frozen`.
Rows 7 to 12 take the cluster lock for their whole sequence (`flock /tmp/wdf-cluster.lock env WDF_CLUSTER_LOCK_HELD=1 bash -c '<commands>'`), and row 14 runs outside it.
The export has no git checkout, so row 7 runs `scripts/tilt/run.sh ci` with `WDF_BUILD_ID` set, which the workflow module requires outside one (`deploy/workflow/README.md:21-22`), as the #7 and #10 exit runs did.
`D` stands for `python -m wdf_admission.drive --kube-context kind-wdf`, which opens its own port-forwards, bound to 127.0.0.1.
Row 10's invariant check opens `kubectl --context kind-wdf -n wdf-infra port-forward --address 127.0.0.1 pod/appdb-0 15432:5432` for the row, builds the superuser DSN from `appdb.credential` in the state directory as the workflow harness does (`harness/cluster.py:61,185`), and closes the forward at the end of the row.

| # | Command | Expected |
| --- | --- | --- |
| 0 | `grep -E '^(MemTotal\|MemAvailable):' /proc/meminfo` and `docker ps --format '{{.Names}}'`, repeated just before row 7 | `MemAvailable` at least the `memory_mib` of the `ci` preset at the run's commit plus 1,024 MiB for the driver, its fake pool workers and the port-forwards, 13,369,344 kB at the 12,032 MiB of `331e013` (`deploy/presets/ci.yaml:8`), else the run stops and records the figure without touching other containers |
| 1 | `uv sync --frozen` then `uv lock --check` | exit 0 for both, lock unchanged |
| 2 | `pytest packages/admission -v -k wheel` | the wheel test passes, importing `wdf_admission` outside the repository |
| 3 | `scripts/check-contracts-map.sh`, `scripts/export-diagrams.sh --check`, then diagrams 10 and 17 opened | exit 0 for each, and diagram 10 shows the refinements of 0.1 (issue), checked by eye because an export gives every file one time |
| 4 | `pytest packages/admission -q -rs` | passes with no `SKIPPED` line |
| 5 | `pytest packages/admission -v -k "claim_commits_before_start or already_started_is_duplicate or rebuilt_accepted_matches_stored or start_options_match_naming or stage_contract_admission or malformed_dead_lettered or unsupported_major_dead_lettered or last_delivery_admission_failed or outage_releases_uncounted or breaker_opens_and_closes or resume_sent_with_row_seq or resume_and_cancel_same_seq_cancel_wins or resumed_before_start_readmitted or stuck_admitted_started or service_caps_hold or service_restart_recovers or flooding_tenant_cannot_starve_quiet_one or claim_sets_a_missing_receipt_once or claim_with_receipt_records_no_published or claim_and_mark_race_records_one_published"` | each named test is listed `PASSED` |
| 6 | `pytest deploy/admission -v` | the render and login tests pass |
| 7 | `just up`, `WDF_BUILD_ID=exit-a scripts/tilt/run.sh ci`, `kubectl --context kind-wdf -n wdf-control get pods`, `kubectl --context kind-wdf -n wdf-control logs deploy/admission`, `just cluster-verify` | exit 0 with `rabbitmq`, `workflow-worker` and `admission` ready among every `ci` module, the log showing admission polling its queues, and an `ok` line for each check of `cluster-verify` |
| 8 | `D smoke --jobs 500 --global-cap 20` | `queue_prefix:` with a fresh `h-<8 hex>-` prefix, `published: 500`, `finished: 500`, `failed: 0`, `max_live:` at most 20, `duplicates: 0` |
| 9 | `D control --jobs 20` | `finished: 20`, `failed: 0`, `skipped: 5` (jobs paused before they started, skipped when their message was pulled), `readmitted: resumed-before-start 5 stuck-admitted 5` (the same five jobs resumed before they started, and five rows the driver left in `admitted` with no workflow), and `resumed: 5` (running jobs paused by signal and resumed through admission) |
| 10 | `D deadletter`, then `python -m wdf_stateops.invariants --dsn <the superuser DSN over the row's forward>` | `dead_lettered: MALFORMED 1 UNSUPPORTED_VERSION 1`, `events: 2` (one `job.dead.lettered` per message that names a job), `rows: accepted 2`, and the invariants print nothing |
| 11 | `D restart --jobs 200`, which deletes the admission pod twice during the run, once with its grace period and once with `--grace-period=0 --force`, then `kubectl --context kind-wdf -n wdf-control get deploy admission -o jsonpath='{.spec.template.spec.containers[0].env[*].name}'` | `finished: 200`, `failed: 0`, `restarts: 2`, `oom_killed: 0` over the two deleted pods and the last pod, read before the driver restores the deployment, and neither `WDF_ADMISSION_GLOBAL_CAP` nor `WDF_POOL_QUEUE_PREFIX` left on the deployment |
| 12 | `just doctor`, then `just down`, then `just nuke` twice | exit 0 for each, and `just nuke` leaves nothing and repeats cleanly |
| 13 | the `ci` workflow on `main` | one green run, the URL recorded in Closure |
| 14 | `just ci` | exit 0, on the full path |

Not provable by this epic: the real dispatch API and its reconciler (#24), real pool workers (#10), which the driver's prefix keeps its jobs away from (Q5) and the smoke of #17 reaches, a second priority class in the shipped configuration (decision 6), the standard load of 100,000 queued jobs and the real caps (#16), crash windows and outages beyond the breaker tests (#15), long jobs with checkpoints holding slots for hours (#18), and the layer 2 backlog gate.

## 8. Parallelism and cost

- Maximum parallel workers: 1 for 0.1, then 2 (P.1 and C.1), then 2 (C.2 beside S.1 then S.2), then 1 each for M.1, M.2, V.1, W.1 and the exit test.
- Host constraints: `wdf_itest` holds the host to two T1 containers through its two lock files, and every T1 test of this epic holds two, so two T1 workers at once queue for the locks.
  The cluster is single-owner, so M.2, V.1 and the exit test queue for the lock, and `ci` needs 12,032 MiB of the 20 GiB host (`deploy/presets/ci.yaml:8`).
- Cluster budget, against the capacity scout's figures: admission takes requests of 50m and 128 MiB and limits of 250m and 128 MiB in `wdf-control` on the infra node, which equals its reserve row, so `scripts/cluster/budget.sh ci` at `331e013`, which counts that row beside RabbitMQ at 320 MiB, holds for admission: the infra node keeps 502 MiB of memory and 220m of CPU free of its requests; every CPU figure is an estimate that no spike measured.
- CI cost, estimates: no new workflow, and `just itest` gains a few minutes for the pairs of T1 containers.
- Estimates, not measurements, in worker-days: lane 0 (1.5), P.1 (2.5), C.1 (2.5), C.2 (3.5), S.1 (2), S.2 (2.5), M.1 (2.5), M.2 (2.5), V.1 (2.5), W.1 (1), exit test (2).
  That is about 25 worker-days of effort and about 19 days elapsed along 0.1, C.1, S.1, S.2, M.1, M.2, V.1, W.1 and the exit test (1.5 + 2.5 + 2 + 2.5 + 2.5 + 2.5 + 2.5 + 1 + 2), with the memory figure of D17 and cluster lock contention as the main sources of slip.

## 9. Closure (filled at close)

<commit sha, exit test run, evidence path or CI run url, what was learned, what moved, which assumptions of section 2 held>

Closure addenda, one dated line each, added after close when a `revisit` child issue is filed against this epic:

- none yet
