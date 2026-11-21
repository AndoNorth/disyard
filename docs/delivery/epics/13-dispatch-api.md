# Epic #24: Dispatch and control API - breakdown

Status: closed 2026-11-19
Epic: https://github.com/AndoNorth/weft/issues/24
Architecture note: docs/architecture/04-intake-dedup-and-dead-letter.drawio, docs/architecture/10-intake-and-admission.drawio and docs/architecture/19-broker-intake-and-dead-letter.drawio today, and the submit and control path diagram `docs/architecture/NN-dispatch-and-control.drawio` that 0.2 (issue) draws, where `NN` is the next free number when it merges, 20 on `main` today because 17, 18 and 19 are the admission cycle, the pool worker and the broker
Verifier: none, per decision 1.
Depends on (closed epics): Dev environment (#1), Fixed-resource cluster (#2), Job event envelope (#4), DAG configuration schema and validator (#5), Durable state and artifact storage (#3), Artifact passing (#12), Workload toolchain (#11), Workflow engine (#6), Broker (#7), Kubernetes workers (#10)
Consumes open work: Admission (#8), whose claim, scans, resume grants, service, module and driver are on `main` [review], and whose epic stays open until its receipt lane and its exit test.

Evidence marks: `[read]` means read on `main` at `6439283` on 2026-11-15 while preparing this spec, and nothing was executed beyond read-only commands.
`[built]` means read on `main` at `331e013` on 2026-11-17, after #7 and #10 closed, while reconciling this spec with them, and nothing was executed beyond read-only commands; where a `[read]` fact and a `[built]` fact differ, the `[built]` one wins.
`[review]` means read on `main` at `15adba0` on 2026-11-18 while applying the spec review, and it wins over both.
The epic issue body and its comment were read on 2026-11-15, and the body is treated as stale where merged work or a recorded decision overtook it, which section 1 lists.
Where the epic body and landed code or a pinned contract disagree, the landed code and the contract win, and this spec says so in the row that applies.
"The realignment" means the alignment review of the open epics after #6 and the decisions taken on it on 2026-11-13, which this spec carries in 0.1.
Method: [docs/delivery/method.md](../method.md).
Model: [docs/delivery/epics/08-workflow-engine.md](08-workflow-engine.md), whose structure this spec follows.
Tiers: T0 is host Python with no runtime, T1 is containers started through `wft_itest` (Postgres, SeaweedFS, the Temporal dev server of the pinned CLI image, and RabbitMQ from `images.rabbitmq` through #7's `wft_broker.testing.container.broker_container`, at most two containers in one pytest session), and T2 is the cluster from `just up` on preset `ci`.
A test that needs a container carries the `integration` marker, a test that needs two containers at once also carries `pair`, because the `t1` fixture sizes its session by that marker, and both run in `just itest` and in every lane's `just ci`.
Command prefixes `nix develop -c` are omitted from every command of this spec and every command takes them, and every Python command also takes `uv run --frozen`.
T2 commands run under `flock /tmp/wft-cluster.lock env WFT_CLUSTER_LOCK_HELD=1 ...` as the cluster epic says.
Every number marked as an estimate is a proposal that a test or the characterization epic (#16) replaces.

## 0. Decisions

Section 0.1 lists what is already decided and carried, 0.2 the open questions, 0.3 the defaults that the lanes build on, and 0.4 the proposals that lane 0 and the issues pin.
A change to a recorded decision changes only what its Applied in column names, and a change to a default only what its Changes column names, where an entry is an issue, a section of this spec or another decision or default.
"0.1 (issue)" and "0.2 (issue)" name the lane 0 issues of section 4, and a bare 0.1 to 0.4 names a part of this section.

### 0.1 Recorded decisions

Everything in the epic body stands except where merged work or the realignment has overtaken it, which this table and section 1 list.

| # | Decision | Source | Applied in |
| --- | --- | --- | --- |
| 1 | No verifier skill, no feature files and no verify recipes for this epic. Every issue names its tests and the exit test is a command checklist | the standing rule of every executed epic (`08-workflow-engine.md:32`), and the realignment, which retired `verify` and `verify-clean` through #366, after which the method records the verifier convention as retired (`docs/delivery/method.md:149-154`) [built] | sections 3, 4, 6, 7 |
| 2 | Python models are the source of anything with a schema, and the exported JSON Schemas are checked in with a drift test | envelope epic decision 5, carried by every later epic (`08-workflow-engine.md:33`) | 0.1 (issue), 0.4 |
| 3 | Runtime libraries read contract files through package data and never by repository path, and every package has a wheel test | state epic decision 11, carried (`08-workflow-engine.md:34`) | 0.1 (issue), 0.2 (issue), every package issue |
| 4 | The API sends only `cancel`, `terminate` and `pause` to a workflow, and never `resume`. Admission sends `resume` with the row's `control_seq` and pending patch while the row's `requested_action` is `resume`. This replaces the epic body's control signal for resume | `08-workflow-engine.md:39` (decision 8), `:86` (D19), `:298-299` (rules rows for #8 and #24) [read] | C.1, C.2, C.3 |
| 5 | The real `SourceResolver` and the run of `resolver_contract` against it belong to this epic, built on `wft_compile.bundle.info(path) -> BundleInfo`. This replaces the epic body's credit of bundle resolution to #5 | `04-validator.md:111`, `:276`, `07-workload-toolchain.md:41`, `:112`, `packages/jobcheck/src/wft_jobcheck/resolver.py:41,88`, `packages/compile/src/wft_compile/bundle.py:25` [read] | B.1 |
| 6 | The `submit` store identity gains `get` and `list` on `wft-sources` through an early lane, because the API reads that bucket to resolve uploaded bundles and to load the base manifest of a patch, and today it holds only `put` and `multipart` | the realignment, decision D7, `contracts/store/roles.yaml:11-12` [read] | E.1, B.1, C.3 |
| 7 | The `jobclass` attribute of an event follows the validator classes `short` and `long`, decided as an early lane with a contract-change PR on the events contract. Q1 settled how strict the check is as its option (a), which E.2 delivered by #429 | #216 and the epic comment of 2026-11-03, `contracts/job/limits.yaml:22` [read], `7959b5c` and `contracts/events/VERSION` at 4 [review] | E.2, Q1 |
| 8 | `INVALID_DAG` is dropped from the dead-letter reasons. `UNKNOWN_TENANT` is a rejection at the API edge only, an API reason code that never becomes a dead letter, so the pinned `job.dead.lettered` reasons (`MALFORMED`, `UNSUPPORTED_VERSION`) stay as they are. The recoverable signal of a dead letter that no event names is #7's, as its Q1 (a) decided: admission dead-letters a `last` delivery itself with the application reason `ADMISSION_FAILED`, the broker's own `delivery_limit` remains for a last delivery lost to a crash, neither writes an event, and an operator recovers both with `python -m wft_broker.dlq replay`, which republishes the message and never touches the row. The reconciler of this epic never republishes a row that holds a receipt, and the API offers no replay route (#7's Q3 (a)) | the realignment, decision D6, `contracts/events/schemas/job.dead.lettered/1.0.json:13-14` [read], `10-broker.md:51-66,80-86`, `contracts/broker/reasons.yaml` [built] | B.2, B.3, Q2 |
| 9 | The priority class stays `standard` for the proof of concept, and the API accepts only the classes of the broker epic's tenancy profile | the realignment, decision D5, `contracts/tenancy/tenancy.yaml:8` [built] | B.2, D17 |
| 10 | Admission re-admits a job that was resumed before it started and a row stuck at `admitted`. The control stage moves such a resumed row back to `accepted` with the row-only `job.resumed`, publishes nothing, and leaves the re-admission to admission | the realignment, decision D8, `contracts/status/status.yaml:39-41,72` [read] | C.1, C.2 |
| 11 | The end-to-end suite drops the two-compile-node check and splits into a core pass and a later pass. The API cases of the core pass (submit, the same key twice, cancel before start, status) are the e2e epic's (#17), and the pause of a running paced job and a patched resume from a checkpoint move to its later pass after #18 | the realignment, decision D3, the #17 body (`#24` Interfaces row) [read] | section 7 |
| 12 | The epic body's "Done means" has three clauses, and each is named here. "Those levels are green" is rewritten as what this epic can prove: the T0 and T1 suites, the 100,000-job call of B.2, which runs outside CI (Q5), the T2 rows of the exit test, and a clean `just ci`, and the level 4 cases of #17 are no longer done criteria of this epic. "`reconcile` of #14 reports no lost and no duplicated job after a resent batch" is no longer a done criterion of this epic. "After any control call the row, the event history and the workflow agree" is kept, and C.2 proves it at T1 with one check per control action | the epic body's own "Done means" depends on #14 and #17, which follow this epic in the method's order (`docs/delivery/method.md:177-183`) [built] | sections 6, 7, B.2, C.2 |
| 13 | Open question 3 is answered: a requested action beside the status, never a status per transition, with `pausing` and `cancelling` as views | `05-state-storage.md:73`, `contracts/status/status.yaml:20-27` [read] | P.1, R.1 |
| 14 | Open question 5 is answered: the submitter declares the job class, the default is `short`, and the submit path writes one value to the row's `job_class` and to the accepted event's `jobclass`. The row has no `class_override` column, so an entry's own class is written into `job_class` and admission passes `class_override` null | `05-state-storage.md:34` (decision 8), `contracts/state/README.md:51-52` [read] | B.2 |
| 15 | Open question 11 is answered: per-job events only, and no batch event. A batch summary is read from the rows | `03-envelope.md:65` [read] | R.1 |
| 16 | One template manifest per batch, stored by hash before any row, with each job's seed, parameters, job class and fingerprint on its row and never in an event | `03-envelope.md:131-141` (the batch manifest interface) [read] | B.2 |
| 17 | The manifest, each source bundle and submitter supplied aggregate index that the template names, and the patched manifest of a resume are indexed and linked to each job that names them before the job row is inserted, except the patched manifest, which is linked after the row exists and before `pending_patch_ref` is set. The index and link rows go through a second connection that logs in as a member of `wft_artifacts` | `06-artifact-passing.md:61-63,229`, `contracts/state/README.md:165` [read] | B.2, C.3 |
| 18 | The two host ports that the cluster epic reserved are used as they are: `control-api` on `127.0.0.1:18080` (NodePort 30080) and `dispatch-api` on `127.0.0.1:18081` (NodePort 30081). The epic body's single port is replaced | `deploy/host/ports.yaml:5-10` [read] | D.2, D16 |
| 19 | A job that never started has no workflow, so the control stage records `job.paused`, `job.resumed`, `job.cancelled` and `job.terminated` for it itself, and it alone emits the four requested events. `job.published` is the dispatch stage's when its publish set the receipt, and admission's when its claim set a missing receipt from a delivered message (lane W of `12-admission.md`) | `contracts/stages/stages.yaml:6-25` [read], `12-admission.md` D9 | B.3, C.1 |
| 20 | Each write runs under its writer's group role: the submit sequence under `wft_dispatch` (`insert_accepted`, `insert_events`, `mark_published`), and control under `wft_control` (`lock_job`, `request_action`, `mark_delivered`, `set_pending_patch`, `transition`, with `row_only=True` for a move the status contract marks so, and `insert_events` for its events, the first and fourth added by E.3). `request_action` checks no status, so the transition table of P.1 decides before it is called | `contracts/state/README.md:156-157`, `packages/stateops/src/wft_stateops/migrations/R__roles.sql:39-48`, `packages/jobstate/src/wft_jobstate/jobs.py:189-209` [read], `R__roles.sql:52` [review] | P.1, B.2, C.1 |
| 21 | The epic body is refreshed after this spec is approved, from sections 0.1 and 1, as each breakdown starts | the realignment, decision D10 | none in this pass |
| 22 | The broker side of every test is #7's own: `wft_broker.testing.FakeBroker` at T0, `wft_broker.testing.container.broker_container` at T1, and `wft_broker.tenancy.load` for tenants and classes at every tier. The `Publisher` port, `FakePublisher`, `publisher_contract`, `StaticTenants` and `tenant_config_contract` that this spec planned as stubs for #7 are dropped, and the publisher takes a `connect(address, credentials)` callable, `wft_broker.client.connect` in the service and `FakeBroker.connect` in tests. This replaces the stubs of the earlier draft | #7 closed on 2026-11-16 with the client, its in-memory fake, the conformance test `broker_contract` that holds the fake and the client to one behaviour, the T1 container and the loader, so a second stub of the same client here would test only itself. `packages/broker/src/wft_broker/testing/fake.py:34`, `testing/container.py:76`, `testing/conformance.py:180`, `tenancy.py:8`, `10-broker.md:659-660` [built] | 0.4, 0.2 (issue), B.3, D.1, D17, section 5 |
| 23 | Publishing follows #7's built client and its rules row for #24. One `Connection` as the broker user `dispatch`, shared by the submit handlers and the reconciler, whose publishes run one at a time, publishes the bytes of the `job.accepted` event, which equal the stored event (D7), with `publish(routing_key, body, timeout_s)` at the default of 5 s, where the routing key is the template `intake.<tenant>.<class>` of the broker contract filled with the row's tenant and priority class (`intake.acme.standard` on `ci`). Only `ACCEPTED` records a receipt. `REJECTED` (a full queue), `UNROUTABLE` (a pair outside the broker topology, logged as a defect), `PublishTimeout` (a memory or disk alarm) and `BrokerUnavailable` leave the row unpublished for the reconciler, and a full queue is never shown to the submitter, whose entry is answered `accepted` before the publish. The first `PublishTimeout` or `BrokerUnavailable` ends the publishing of a submit call and the reconciler's pass, because an alarm holds every publish and a lost connection fails every pending call. After `BrokerUnavailable` the publisher calls `connect` again before its next publish, because the client never reconnects by itself. `TopologyError` stops the publisher and the reconciler with an error log and no retry | `packages/broker/src/wft_broker/client.py:53,260`, `contracts/broker/topology.yaml:8`, `10-broker.md:117,169-176,255` [built] | D7, D8, B.3 |
| 24 | The service's module is named `dispatch`, the name of the reserve row that the capacity revisit put in the budget check, so the check counts the module in place of the row and the `ci` infra node keeps the 502 MiB free that #7's closure measured. D.2 also moves `dispatch` out of the reserve loop of `test_ci_infra_node_figures`, which asserts that the row is present. This replaces the module name `dispatch-api` of the earlier draft, while the host ports keep their names `dispatch-api` and `control-api` | the fallback of the earlier draft's A10, `scripts/cluster/budget.sh:35-39`, `scripts/cluster/test_budget.py:43,49`, `10-broker.md:704` [built] | D1, D16, D.2, section 7 |
| 25 | The T2 rows of the exit test run on #10's pool workers, which poll the unversioned contract queues `wft-compile`, `wft-simulate` and `wft-aggregate`. A job reaches them only when admission starts it with no `wft_pool_queue_prefix` memo (#10's D22), and nothing this API writes sets one. Their jobs use the workload library bundle (`python -m wft_workloads.bundle`, entry `wft_workloads.main`), as the harness's `--pools real` does, because a bundle without a `Run` export fails as `compile_failed`. A manual pause withdraws a running task only when it is a simulate task whose plan sets `checkpoint`, and that task returns to `ready` with no checkpoint until #18; every other running task runs to completion before the job projects `paused` | `11-workers.md:71,84`, `packages/workflow/src/wft_workflow/ready.py:68-72`, `taskpath.py:52`, `08-workflow-engine.md:38` [built] | A8, D.2, section 7 |

### 0.2 Questions

None of these blocks lane 0, the early lanes or lane P.
Q1 and Q5 are settled, and their rows are kept as the record.
Each has a default that the lanes build on when there is no answer, the default changes no pinned contract beyond the events change that decision 7 already makes, and an answer that differs from the default changes only the issues named in the last column.

| # | Question | Options | Recommendation | Default if no answer | Changes |
| --- | --- | --- | --- | --- | --- |
| Q1 | Settled as (a) by E.2, delivered by #429 (`7959b5c`, events v4) [review]. How strict is the job class check of #216? Every event decoded through `wft_events.from_bytes` is checked against the profile schema (`packages/events/src/wft_events/codec.py:66`), and `JobWorkflow` builds its own events with `wft_events.build_event` from the accepted event's `jobclass` (`packages/workflow/src/wft_workflow/job.py:70,85`), also while it replays. The recorded histories of `packages/workflow/corpus/v1/` and of `packages/temporal` carry `jobclass: simulate`, so a check in the profile, the model or `build_event` fails those replays | (a) refuse a class outside `classes` at the only producer of the accepted event: the dispatch API answers `UNKNOWN_JOB_CLASS` and never builds one, every later event copies the class from it, the events contract states the vocabulary, its 111 vectors are rewritten from `simulate` to `short`, and the profile, the model and `build_event` keep accepting any class name, so recorded histories and stored rows still decode and replay; (b) refuse it everywhere: the profile schema takes the enum, the model takes a literal, `build_event` raises, and both replay corpora and every test fixture are re-recorded | (a). It gives the guarantee #216 asks for at the one place a class enters the system and keeps the determinism gates of #4 and #6 as recorded. The departure from the literal wording of the epic comment ("make the model reject") is that the check lives in the API and not in `wft_events` | (a). With (b) E.2 becomes a `revisit` child of #4 that also owns the two corpora, their `owners.yaml` and the fixtures, and the re-recording is a change to two closed epics' gates | none, settled |
| Q2 | How does a dead-lettered job show in the API? A job whose intake message was dead-lettered keeps its row at `accepted` with its receipt (`contracts/status/status.yaml:49-50`), so a status read shows a job that looks queued. Under #7's decided Q1 (a), two of the four reasons a job can be dead-lettered for write `job.dead.lettered` (`MALFORMED`, `UNSUPPORTED_VERSION`) and two write no event (`ADMISSION_FAILED`, `delivery_limit`), whose only record is `intake.dlq`, read by an operator with `python -m wft_broker.dlq list` (`contracts/broker/reasons.yaml`, `10-broker.md:51-66`) [built] | (a) the status read adds `dead_letter: {dlq_reason, detail, time}` from the latest `job.dead.lettered` event of the job, read through the reader login of D13, and `null` otherwise, so an `ADMISSION_FAILED` or `delivery_limit` dead letter reads as a queued job; (b) nothing in the API, and dead letters are seen only through the broker's tooling; (c) as (a), and #8 files #7's Q1 (b), an events contract-change PR adding `ADMISSION_FAILED` to `job.dead.lettered` 1.1 and writing it, which #7 recommends "once #24's status must show a dead letter", so the field also shows an admission failure, and only a `delivery_limit` death, a last delivery lost to a crash, stays without an event | (a). A clear reason next to the job is what recovery needs, it costs one indexed read, and it adds nothing to a pinned contract because the status response is this epic's own. Whether the status must also show `ADMISSION_FAILED`, which (c) gives at the cost of an events bump and work in #8, is the captain's call with #8 | (a). With (b) R.1 drops one field. With (c) `test_dead_letter_field` of R.1 gains an `ADMISSION_FAILED` case, and #8 and the events contract carry the rest | R.1, 0.1 (issue) |
| Q3 | Are `terminate` and a patched `resume` operator-only? The epic leaves it open (open question 4) and decides no authentication for v1 | (a) no distinction in v1: any caller on 127.0.0.1 may call every route of its tenant, as for every other call; (b) a static operator token per tenant from a Secret, required on `terminate` and on a resume with a patch | (a). The tenant is self-asserted on loopback, so a token on two routes protects nothing that the other routes leave open | (a). With (b) D.2 adds a Secret, 0.1 (issue) a reason code, and C.1 and C.3 a header check with the reason `OPERATOR_TOKEN_REQUIRED` | C.1, C.3, D.2, 0.1 (issue) |
| Q4 | Does the API cap the pending jobs of a tenant? The epic leaves it open (open question 6) | (a) no cap at the API: admission's quotas bound what runs, the bounded intake queue bounds what is published, and the rest waits in Postgres; (b) a static per-tenant cap on rows at `accepted`, rejecting further entries with `TENANT_PENDING_CAP` | (a). The design keeps a large backlog in Postgres on purpose (the epic's key decision on unbounded dispatch), and a cap needs a number that only #16 can measure | (a). With (b) B.2 adds a count per chunk and 0.1 (issue) a reason code, and the cap is a figure of D10, because tenancy v1 holds no per-tenant pending figure (its tenant carries only `quota`, live workflows, which is admission's) and a new field there is a contract-change PR of #7 | B.2, 0.1 (issue) |
| Q5 | Which batch size does this epic prove? The limit stays 100,000 entries per call. Decided by the captain in the spec review of 2026-11-18, beyond both options: B.2's `test_submit_100000` proves one call of 100,000 jobs at T1 and its resend through the submit route, runs outside CI, and is run with its output pasted before the PR that closes B.2 merges, while the 10,000-job test stays in CI and S7 still measures the library calls | (a) the exit test proves one call of 10,000 jobs at T1 and a resend of it, and the optional spike S7 measures one call of 100,000 jobs at T1 and hands its numbers to #16; (b) the exit test proves 100,000 jobs per call at T1 | (a). 10,000 already crosses ten chunks and every crash window, S7 measures the full size without making the exit test depend on host speed, and #16 fixes the numbers either way | (a). With (b) row 7 of the exit test takes 100,000, which is at least 2.5 minutes of T1 time on this host for the rows and their events alone by the state epic's baseline of 979 rows and 1,980 events per second (`05-state-storage.md:490`), before links and publishes (an estimate) | B.2, section 7 |

### 0.3 Defaults the lanes build on

These carry the remaining open questions of the epic and the choices found while reconciling it.
Numbers marked as estimates are fixed by a test named in the row or by #16.

| # | Topic | Default | Reason, and the alternative | Changes |
| --- | --- | --- | --- | --- |
| D1 | Service shape for the two ports | One Deployment `dispatch` (decision 24) with one replica and the `Recreate` strategy, running one process `python -m wft_dispatchapi.service` with two HTTP listeners: `dispatch-api` (container port 8081, NodePort 30081) serves uploads, batch submit and batch reads, and `control-api` (container port 8080, NodePort 30080) serves control and the job status read. The reconciler (D7) and the control retrier (D6) are loops in the same process, answering open question 9 | The cluster budget holds one dispatch reserve of 256 MiB (#366), the submit path and control race on the same row, and the epic's key decision is one API. The alternative is two Deployments, one per port, which takes a second 256 MiB from the 502 MiB that the 6,144 MiB infra node keeps free once the `rabbitmq` module and the reserves are counted (`10-broker.md:704`) [built], and leaves 246 MiB for every later step-up of a control or infra module | D.1, D.2 |
| D2 | HTTP server | The standard library's `http.server.ThreadingHTTPServer`, one per listener, with at most 16 requests in flight per process (an estimate), a request body read as a stream from `rfile`, and an NDJSON answer written with chunked transfer encoding. No route table library and no OpenAPI document: the route table, the models and their generated schemas in `contracts/api/` are the description | No HTTP server package is locked in `uv.lock` today [read], and a stream of lines needs nothing more than the standard library. The alternative is Starlette and Uvicorn, two new locked dependencies and an event loop for every handler | D.1 |
| D3 | Temporal client | One `temporalio` client on one asyncio loop in a dedicated thread, connected with `wft_temporal.converter.event_data_converter` and `wft_temporal.trace.tracing_interceptor()` as the workflow service does, and called from handler threads through `asyncio.run_coroutine_threadsafe` behind a `WorkflowGateway` port (0.4) | The SDK client is asynchronous and the handlers of D2 are threads. `packages/workflow/src/wft_workflow/service/__init__.py:159-164`, `packages/temporal/src/wft_temporal/converter.py:63`, `packages/temporal/src/wft_temporal/trace.py:39` [read] | D.1, C.2, R.1 |
| D4 | Has the job a workflow? | A row at `accepted` has no workflow, because a row leaves `accepted` for a status with a workflow only by admission's claim, and comes back only by the row-only move from `resume-pending`, which is made only for a row with no workflow (D6). For a row at `paused` or `resume-pending`, the control stage asks the gateway whether `workflow_id(job_id)` exists, and the answer is stable while the row stays there, because admission claims only `accepted` rows. A row at `admitted` or `running` is treated as having a workflow, and a signal that finds none is retried (D6). When Temporal does not answer for a row at `paused` or `resume-pending`, a `cancel` or `terminate` is recorded with outcome `requested` and D6 finishes it, and a `resume` is refused `TEMPORAL_UNAVAILABLE` | `wft_control` has no grant on the event payloads that would show a `job.admitted` (`R__roles.sql:53`), and Temporal's answer is the fact the signal path depends on. The alternative is a state contract change granting `wft_control` the event columns `type` and `jobid` | C.1, C.2 |
| D5 | Event ids of the control stage | Every event of the control stage, the four requested events and the four row-only events of a job that never started, takes `sequence` equal to the request's `control_seq`, and its id is `wft_keys.cloudevent_id(job_id, "", "", <type>, control_seq)` through `wft_events.build_event`. The accepted and published events take sequence 0 | One request writes each event type at most once, and a new request takes the next sequence, so the ids are unique within `/wft/control`. The stored key is `<source>#<id>` with `event_time` (`contracts/state/README.md:58,62`), so a control event never meets a workflow event of the same type, whose sequence is the resume counter (`packages/workflow/src/wft_workflow/job.py:251-252,376-378`), even though the id formula has no source input (`packages/events/src/wft_events/build.py:50`). The alternative, an offset for the row-only events, guards against a collision the store cannot have | P.1, C.1 |
| D6 | Control retrier | A loop every 1 s (an estimate) selects up to 100 rows (an estimate) whose `requested_action` is `cancel`, `terminate` or `pause`, whose `control_delivered_at` is null and whose status is `admitted`, `running`, `paused` or `resume-pending`, sends the signal of `naming.yaml` with `ControlSignal(control_seq, reason)`, and calls `mark_delivered(job_id, control_seq)` once the server accepted the signal. The `reason` is read from the stored requested event through the reader login of D13, because `wft_control` cannot read event payloads. A signal that finds no workflow, or a Temporal outage, backs off per row from 1 s doubling to 60 s (estimates) and never gives up while the action is outstanding and the row is not terminal. For a row at `paused` or `resume-pending` with no workflow, it finishes a `cancel` or `terminate` itself under `lock_job` by the move to the terminal status with its event, a move that the status contract allows without `row_only`. A second loop, the resume finisher, runs every 60 s (an estimate) over the rows whose `requested_action` is `resume` and whose status is `resume-pending`, reads up to 100 of them (an estimate) in job id order after the last job id it read, starting from the first again at the end, asks the gateway whether each has a workflow (D4), and for each that has none makes the row-only move to `accepted` with `job.resumed` under `lock_job`. On the cells of the control table only a job that started reaches that state: a job that never started makes both of its resume moves in one transaction, and D4 refuses its resume when Temporal does not answer, so the row is one whose workflow was closed from outside its own code and then purged after the namespace's 24 h retention (`deploy/temporal/README.md:34`). Back at `accepted` with its receipt, the row is re-admitted by admission (the rules row for #8), so the job starts again from its batch template. `resume` is never signalled (decision 4) | The row write and the signal cannot share a transaction, and a cancel lost during an outage would leave a job running. The workflow applies a repeat as a no-op (D19 of the workflow epic), so a signal sent twice after a crash before `mark_delivered` is harmless. The resume finisher has its own selection and cadence because every `resume-pending` row that waits for admission's grant also has `resume` outstanding and `control_delivered_at` null, so the alternative, adding `resume` to the 1 s selection, would fill it with existence checks and crowd out the delivery of cancels and pauses | C.2 |
| D7 | Reconciler | A loop every 5 s (an estimate) selects rows at `accepted` with `published_at` null whose `created_at` is older than 10 s (an estimate, twice the 5 s publish timeout of decision 23, so a submit that publishes its rows promptly is seldom raced), oldest first, up to 200 rows per second (an estimate). The selection filters on the status as well as on `published_at`, because the partial index `accepted_job_unpublished` has no status term and keeps every row cancelled or paused before its publish, which each pass reads past. For each row it rebuilds the `job.accepted` event from the row with `time` equal to `created_at`, which is the stored event byte for byte because the submit builds it with that time (D8), inserts it with `insert_events`, which stores nothing while the stored copy exists and stores it again once the event history has dropped it (14 days, `contracts/state/retention.yaml:6`), and publishes it under the publish rules of decision 23, so a pass ends at the first `PublishTimeout` or `BrokerUnavailable`. After a `REJECTED` the pass offers that intake queue none of its later rows, because a full queue rejects every publish until it falls well below its `max-length` (`10-broker.md` D5), while the rows of other queues are still published. After `ACCEPTED`, `mark_published` sets the receipt only when it is null, and only the call that set it records `job.published`, with the receipt time (E.3). The reconciler can race a submit that is still publishing the late rows of a chunk, whose `created_at` is the start of the chunk's transaction, and it can race admission's claim of a message that waited in its queue. Either race is harmless: the receipt is set once, by dispatch or by admission's claim of a delivered message (`12-admission.md` D9), whichever set it records the one `job.published`, and admission acknowledges and counts the second message (the dedup chain of section 1). A row with a receipt is never republished, and a row moved off `accepted` before its publish is published when it comes back to `accepted`, by the row-only resume | Republishing a rebuild reads no event payload, so the reconciler needs no reader login, and the rebuild is the stored event because both are built from the row with the same time, which is the rule admission's `accepted_of` already follows (`packages/admission/src/wft_admission/claim.py:39-57`) [review]. The alternative, reading the stored event through the reader login and rebuilding only once it was dropped, keeps two paths to the same bytes. The partial index is #3's (`packages/jobstate/src/wft_jobstate/migrations/0100_accepted_job.sql:31-32`) [review], and the alternative, a status term in it, is a `revisit` of #3 for a cost that #16 measures first | B.3, D15 |
| D8 | Submit sequence per chunk | For each chunk of entries, in order: validate each entry and derive its key, job id and fingerprint; then, on the artifacts connection, `index_reference` each content object the batch names once per call, `link_use` it for each job of the chunk, and commit; then, on the dispatch connection, in one transaction, `insert_accepted` the rows and `insert_events` one `job.accepted` per inserted row, built with `time` equal to that transaction's `now()`, which is the rows' `created_at`, so the reconciler's rebuild is byte-equal (D7), and commit; then write one answer line per entry; then publish each inserted row under the rules of decision 23 and, for each `ACCEPTED` outcome, call `mark_published` and, only when it returns a time, insert `job.published` with that time, in one short transaction. `mark_published` returns no time when the receipt is already set, by the reconciler or by admission's claim of a delivered message (`12-admission.md` D9), and then the call records nothing. Any other outcome or error leaves the row to the reconciler, and after the first `PublishTimeout` or `BrokerUnavailable` the call publishes nothing more. A `link_use` that answers `RowPurged` for an object uploaded before the call stops the call with `OBJECT_MISSING` before any row of the chunk, and for the batch manifest the API stores it again and retries once. A crash after the links and before the rows leaves links for jobs with no row, which the reaper's grace rule covers | The links must exist before the row (decision 17), the two connections cannot share a transaction, and publishing after the answer lines keeps the answer bounded by Postgres and not by the broker (the epic's key decision) | B.2, B.3 |
| D9 | Keys and fingerprint | The batch key is required. The job key is the entry's `submission_key`, else `<batch key>/<index>`, and no key is ever generated. The batch id is `wft_keys.batch_id(tenant, batch_key)` and the job id `wft_keys.job_id(tenant, key)`. The fingerprint is the lower-case hex SHA-256 of the UTF-8 bytes of the canonical JSON (sorted keys, no whitespace, no ASCII escaping) of `{"job_class", "manifest_sha256", "parameters", "priority_class", "seed"}`, pinned with vectors in `contracts/api/` | A generated key is not repeatable, so a resend would create new jobs, which the epic itself records as a hazard. No fingerprint function exists on `main` [read], and the row only requires 64 hex characters (`contracts/state/README.md:42`) | P.2, B.2, 0.1 (issue) |
| D10 | Batch limits (open question 6) | 100,000 entries per call with a typed rejection above it; 2 KiB per entry line; a header line of at most 1,052,672 bytes (the 1 MiB `max_document_bytes` of the template plus 4 KiB); 2 submit calls in flight per tenant and 4 per process; a 60 s idle timeout on reads and a 60 s timeout on each answer write, and no total limit; 1,000 entries per chunk. The contract states that a client reads the answer lines while it sends, because the server answers each chunk before it reads the next, and a client that sends the whole body first is stopped by the write timeout with every answered entry durable. All are estimates that #16 fixes | The epic's proposals, kept. An invalid header rejects the call before any row, and anything else is a per-entry result | P.2, B.2 |
| D11 | Upload of content objects | `PUT /v1/tenants/{tenant}/objects/{kind}/{sha256}` for kind `source` or `index`, streamed into `wft-sources` with `put_content(source, kind, name)` and no connection, at most 2 uploads at a time per process because `put_content` spools a stream to a temporary file (`packages/artifactclient/src/wft_artifactclient/client.py:161`), the hash and the kind cap of the staging contract (`source` 64 MiB, `index` 128 MiB, `contracts/artifact/staging.yaml:20-23`) checked on receipt, and a source bundle refused unless `wft_compile.bundle.info` reads it. The object is indexed and linked to each job only when a submit names it (D8). An object never named stays unindexed in `wft-sources` | `put_content` takes a connection only together with a `job_id` (`packages/artifactclient/src/wft_artifactclient/client.py:301-302`), and no job exists at upload time, which is the gap the realignment found between the epic body and #12. Sources have no age rule (`contracts/state/README.md:181`), so an unused upload is a leak, which the epic's risk list already accepts | B.1 |
| D12 | Template and manifest | The batch header carries the template document inline. The API validates it once per call with `wft_jobcheck.validate(data, profile, resolver)`, compiles it with `wft_plan.compile_plan`, stores `wft_plan.canonical_bytes(plan)` as kind `manifest` with `put_content` before the first chunk, and indexes and links it per job as in D8. Each entry is checked with `wft_plan.check_job_params(plan, params, profile)` under the `ci` limits profile | One shared template per batch is decided (decision 16), and an inline document needs no second upload for the common case | B.2 |
| D13 | Database logins | Four service logins created by the module, as the workflow module does with its logins: one in `wft_dispatch` for the submit sequence, one in `wft_control` for control, one in `wft_artifacts` for index and link rows, and one in `wft_reader` for reads of event payloads (status results, the dead-letter field of Q2 and the reason of a requested event that the retrier of D6 signals) | The group roles cannot log in (`R__roles.sql:5`), and `wft_dispatch` and `wft_control` read only `event_id` and `event_time` of `event` (`R__roles.sql:53`), while `wft_reader` reads every table (`R__roles.sql:74-75`) | D.1, D.2 |
| D14 | Status read | The row, the view derived from `requested_action` as `contracts/status/status.yaml:20-27` says, the result references of the job from its `task.result.ready` events, and for a row at `admitted`, `running`, `paused` or `resume-pending` the workflow's `status` query with a 2 s timeout (an estimate). A query that fails or times out leaves `detail: null` with `detail_error: unavailable`, and a missing workflow gives `detail_error: no-workflow`. The read never fails because Temporal is down | The epic's "row first, plus a workflow query" | R.1 |
| D15 | Metrics | The service exposes Prometheus metrics on a container port 9090 that has no NodePort, named under the instrumentation contract: `wft_dispatch_jobs_total` by `outcome` and `reason`, `wft_dispatch_submit_seconds`, `wft_dispatch_unpublished_jobs`, `wft_dispatch_unpublished_oldest_seconds`, `wft_control_requests_total` by `action` and `outcome`, `wft_control_outbox_jobs` and `wft_control_outbox_oldest_seconds`. No label holds a job, batch or tenant id. The two `wft_dispatch_unpublished_*` gauges count rows at `accepted` with no receipt, filtered on the status as the reconciler's selection is (D7). The observability epic adds them to `metrics.yaml` and owns the alerts. The broker client also counts each publish as `wft_broker_publish_total{outcome}` in the in-process registry of `wft_events.metrics`, pinned by #7 at instrumentation v2, which #13 exports from this process as #7's D15 assigns, so this service adds no exporter for it | The #13 body lists exactly these for #24, with the backlog gauge requested [read]. Adding to `metrics.yaml` here would take over a file that #13 is about to own. `contracts/instrumentation/metrics.yaml:28`, `10-broker.md:122` [built] | D.1 |
| D16 | Module | `deploy/dispatchapi/`, module name `dispatch` (decision 24) in `wft-control`, group `control`, pin `python`, requests 100m and 256 MiB, limits 500m and 256 MiB (estimates equal to the `dispatch` reserve row of `scripts/cluster/budget.sh:37`), two NodePort Services on 30081 and 30080, the four logins of D13, the `submit` store identity from the state directory, the broker address `rabbitmq.wft-infra.svc.cluster.local:5672` (the Service `rabbitmq` of #7), a Secret of its own in `wft-control` holding the password of the broker user `dispatch` that `uv run --frozen python deploy/rabbitmq/config.py password dispatch` returns and creates on first use, `WFT_TENANCY_PROFILE` mapped from `WFT_TILT_PROFILE` through `tilt_profiles` of the tenancy contract as the `rabbitmq` module maps it, an image build stage that builds `python-qpid-proton` 0.40.0 from its sdist with `gcc` and `libc6-dev` (`packages/broker/README.md`), a read-only root with a disk-backed `/tmp` emptyDir of 512 Mi (two spooled `index` uploads of 128 MiB with room to spare, where the workflow module's 64 Mi would evict the pod, `deploy/workflow/Tiltfile.module:101`), and readiness on the Deployment. It depends on `infra/appdb-migrate`, `infra/objectstore-bootstrap`, `infra/temporal-bootstrap` and `infra/rabbitmq` | The workflow worker's module is the pattern (`deploy/workflow/module.yaml`) [read]. The broker address, the password helper, the Tilt profile mapping and the proton build are #7's hand-off to a publisher (`deploy/rabbitmq/README.md`, `contracts/tenancy/README.md`, `10-broker.md:255`) [built]. The budget check ignores a reserve row once a module of that name exists, so the module takes the row's name (decision 24) | D.2 |
| D17 | Tenant and class check | The API loads the tenancy profile once at start, through `wft_broker.tenancy.load` with the profile named by the environment variable `WFT_TENANCY_PROFILE` (`ci` on preset `ci`), refuses a tenant outside its `tenants` with `UNKNOWN_TENANT` before reading a body, and refuses a priority class outside its `priority_classes` per entry with `UNKNOWN_PRIORITY_CLASS`. Tenancy v1 also holds `reserve_interval`, `caps.open`, `caps.global`, `caps.job_class` and each tenant's `quota`, which are admission's and which this epic never reads, so this epic needs no field the profile lacks and adds none | Decisions 8, 9 and 22. The tenant scopes every route through the path, so a read or a control call names its tenant and never sees another tenant's job. `contracts/tenancy/tenancy.yaml:5-20`, `contracts/tenancy/README.md` Consumers [built] | 0.2 (issue), B.2, C.1, R.1 |
| D18 | Resume with a patch | A resume without a patch clears `pending_patch_ref`, so the workflow keeps the plan in force (`ResumeSignal.manifest` null, `packages/workflow/src/wft_workflow/job.py:258`) and a patch the workflow refused is never sent again. The body carries the full patched template document. For a job with a workflow, the API validates it as in D12, loads the base plan from the row's `pending_patch_ref` when set and else from `manifest_ref` (reading `wft-sources` under decision 6), takes the completed tasks (`succeeded` or `skipped`) from the status query, runs `wft_jobpatch.patch_check(old, new, completed)` as a pre-check, the workflow's own check when it loads the patch being the one that decides, stores the patched manifest with `put_content(..., conn, job_id)` on the artifacts connection, and then, in one control transaction, requests `resume`, moves `paused` to `resume-pending`, sets `pending_patch_ref` through the writer of E.3 and inserts `job.resume.requested`. A job that never started takes no patch and is refused with `PATCH_NOT_STARTED`, because its start uses the batch template and `manifest_ref` is not the control role's to change. Without Temporal a resume with a patch is refused with `TEMPORAL_UNAVAILABLE` and writes nothing | `wft_control` may update `pending_patch_ref` (`R__roles.sql:48`), but no library call writes it [read], which is why E.3 exists. The alternative for a job that never started is to have admission start it with the pending patch, which changes the start contract of #8 and #6 | C.3, E.3 |
| D19 | Batch scope of control | `POST .../batches/{batch_key}/{action}` applies the single-job rule of P.1 to every job of the batch, one transaction per job under `lock_job` as the control table says, reads the batch's jobs in pages of 1,000 (an estimate) that serve only the paging, and answers one NDJSON line per job after its transaction commits, then a summary line | #15 cancels the running jobs of a batch, and one rule for both scopes keeps one tested function. One transaction per job holds one row lock at a time, so admission's claim waits on at most one job, the Temporal call of D4 never runs while other jobs stay locked, and every answered job is committed. The alternative, one transaction per page as submit commits per chunk, holds up to 1,000 locks and rolls back jobs whose answer lines may already be written | C.1 |
| D20 | Package | `packages/dispatchapi/`, import `wft_dispatchapi` | `wft_dispatch` is already the name of a database group role, and two meanings for one name would confuse every grant review | 0.2 (issue) |

### 0.4 Proposals the lanes pin

After lane 0 merges, `contracts/api/README.md` and its files are the source and this section is only a pointer.
The tables after this one give every route, model, reason code and control cell that 0.1 (issue) pins.

| Item | Proposal | Epic item |
| --- | --- | --- |
| Contract directory | One contract, `contracts/api/` (contract 6 of the method, API half), owner #24, version 1, with `VERSION`, `README.md`, `check.sh`, `routes.yaml`, `reasons.yaml`, `control.yaml`, `vectors/fingerprint.json`, the generated schemas and vectors under `vectors/<name>/{valid,invalid/<reason>}/`. Models live in `wft_dispatchapi.models`. Loader `python -m wft_dispatchapi.contract --check FILE [--as NAME]` (exit 0 valid, 1 with `reason: <code>: <detail>` on stderr, 2 usage), generator `python -m wft_dispatchapi.gen [--check]`. The tenant and class configuration is the other half of contract 6, pinned by the broker epic as `contracts/tenancy/` at tenancy v1 | method contract 6, detail issue 1 |
| Routes | `routes.yaml`, the table below, each route on exactly one listener | detail issues 1, 2, 4 (upload), 6, 10 |
| Bodies | NDJSON in and out for batch submit and batch control (`application/x-ndjson`), JSON for every other route, and raw bytes for an upload. HTTP bodies are never CloudEvents | the epic's key decision on bodies |
| Reason codes and error body | `reasons.yaml`: one row per code with its scopes (`call` for a batch submit call, `entry`, `upload`, `control`, `read`, or `any` for every route) and HTTP status. Every error answer is `{code, message, trace_id}` | detail issue 1 |
| Control table | `control.yaml`: one cell per (state, action), with the outcome, the row writes and the events, and the overlay rules for an outstanding action, as below. P.1 implements it as a pure function and a test holds the function equal to the file | detail issue 6, open question 3 |
| Fingerprint | the formula of D9, with input and output vectors | detail issue 3 |
| Trace context | each job gets its own `traceparent` with a fresh trace id, stored on the row and in its accepted event, and returned as `trace_id` in its answer line. A call has one root span whose links name the jobs of its first chunk | the epic's "root trace context per job" |
| Stubs and conformance tests | `wft_dispatchapi.testing`: `FakeGateway` and `gateway_contract(gateway)`, as section 5 states. The broker and the tenancy profile are #7's own `FakeBroker`, `broker_container` and `wft_broker.tenancy.load` (decision 22) | the stub for Temporal |

#### Routes

All paths start with `/v1/tenants/{tenant}`, which is shortened to `T` below, and a tenant outside the configuration answers 404 `UNKNOWN_TENANT` before any body is read.

| Listener | Method and path | Request | Answer | Success |
| --- | --- | --- | --- | --- |
| dispatch-api | `PUT T/objects/{kind}/{sha256}` | raw bytes of a `source` bundle or an `index` object | `ObjectAnswer` `{reference, created}` | 201 created, 200 already present |
| dispatch-api | `POST T/batches` | NDJSON: one `BatchHeader` line, then `BatchEntry` lines | NDJSON: one `EntryAnswer` per entry in request order, then one `SummaryLine`, or an `ErrorLine` last when the call stops | 200 |
| dispatch-api | `GET T/batches/{batch_key}` | none | `BatchSummary` `{batch_id, total, counts}`, `counts` keyed by status | 200 |
| dispatch-api | `GET T/batches/{batch_key}/jobs?status=&after=&limit=` | none | `JobPage` `{jobs: [{job_id, submission_key, status, view}], next}`, ordered by job id, `limit` from 1 to 1,000 | 200 |
| dispatch-api | `GET T/depth` | none | `Depth` `{depth}`, the rows at `accepted` keyed by priority class then job class | 200 |
| control-api | `GET T/jobs/{job_id}` | none | `JobStatus` | 200 |
| control-api | `POST T/jobs/{job_id}/{action}` with `action` in `cancel`, `terminate`, `pause`, `resume` | `ControlRequest` `{reason, template}`, `template` allowed only on `resume` | `ControlAnswer` `{job_id, action, outcome, status, view, control_seq}` | 200 |
| control-api | `POST T/batches/{batch_key}/{action}` | `ControlRequest` without `template` | NDJSON: one `ControlAnswer` or `EntryError` per job, then one `SummaryLine` | 200 |
| both | `GET /healthz`, `GET /readyz` | none | `{status}`; ready means the database logins connect, the store answers a `head`, and the Temporal client is connected | 200, or 503 when not ready |

Models:

| Model | Fields and rules |
| --- | --- |
| `BatchHeader` | `batch_key` (1 to 200 characters, so `<batch key>/<index>` fits the 256 of `submission_key`), `template` (an object of at most `max_document_bytes`), `priority_class` (default `standard`), `job_class` (default `short`) |
| `BatchEntry` | `index` (0 to 99,999, unique within the call), `submission_key` (1 to 256 characters, optional), `seed` (0 to 2^53 - 1), `parameters` (an object), `priority_class` and `job_class` (optional overrides of the header) |
| `EntryAnswer` | `index`, `outcome` (`accepted`, `duplicate`, `rejected`), `job_id` (absent only when the entry could not be parsed far enough to derive it), `status` (on `duplicate`, the existing row's status), `reason` (on `rejected`), `issues` (on `INVALID_PARAMS`, at most 10 validator issues with `path` and `code`), `trace_id` |
| `SummaryLine` | `summary: {batch_id, accepted, duplicate, rejected}` for submit, and `summary: {applied, requested, noop, refused}` for batch control |
| `ErrorLine` | `error: {code, message, trace_id}`, written as the last line when a call stops after it began answering. Every entry answered before it is durable, and a resend of the whole call is safe |
| `EntryError` | `job_id`, `submission_key`, `code`, `message`, one line of a batch control answer for a job whose action was refused |
| `ControlAnswer` | `outcome` is `applied` when the row reached the target, `requested` when the request waits for the workflow or admission, `noop` when nothing changed |
| `JobStatus` | `job_id`, `tenant`, `batch_id`, `submission_key`, `status`, `view` (`pausing`, `cancelling` or null), `requested_action`, `control_seq`, `control_delivered`, `priority_class`, `job_class`, `published`, `created_at`, `updated_at`, `terminal_at`, `trace_id`, `detail` (a `StatusQuery` of the temporal contract or null), `detail_error` (`unavailable`, `no-workflow` or null), `results` (the result references of the job's `task.result.ready` events, in task id order), `dead_letter` (Q2) |

#### Reason codes

| Code | Scope | HTTP | When |
| --- | --- | --- | --- |
| `UNKNOWN_TENANT` | any | 404 | the tenant of the path is not in the configuration (decision 8) |
| `INVALID_HEADER` | call | 400 | the header line does not parse or breaks `BatchHeader` |
| `HEADER_TOO_LARGE` | call | 413 | the header line is above its limit (D10) |
| `INVALID_TEMPLATE` | call | 400 | the validator refuses the template, with its issues in `message` |
| `TOO_MANY_ENTRIES` | call | 413 | an entry line beyond the limit arrives; every entry before it is answered |
| `TOO_MANY_CALLS` | any | 429 | the tenant or the process already has its limit of submit calls in flight, or the process has its limit of requests |
| `IDLE_TIMEOUT` | call | as an `ErrorLine` | no byte arrived for the idle timeout, or an answer write did not finish in its timeout, when the line may not reach the client |
| `MALFORMED_LINE` | entry | in its answer line | an entry line is not a JSON object or breaks `BatchEntry` |
| `ENTRY_TOO_LARGE` | entry | in its answer line | an entry line is above its limit |
| `DUPLICATE_INDEX` | entry | in its answer line | the index was used earlier in the call |
| `UNKNOWN_PRIORITY_CLASS` | entry | in its answer line | the class is not in the configuration |
| `UNKNOWN_JOB_CLASS` | entry | in its answer line | the class is not in `classes` of the limits profile |
| `INVALID_PARAMS` | entry | in its answer line | `check_job_params` reports issues |
| `KEY_CONFLICT` | entry | in its answer line | the key exists with another fingerprint, and nothing changed |
| `BAD_KIND` | upload | 400 | the kind is not `source` or `index` |
| `HASH_MISMATCH` | upload | 422 | the received bytes hash to another value than the path |
| `OBJECT_TOO_LARGE` | upload | 413 | the bytes pass the kind cap of the staging contract |
| `UNREADABLE_BUNDLE` | upload | 422 | `wft_compile.bundle.info` refuses a source bundle |
| `OBJECT_MISSING` | call | 409 | an object the template names was purged after its upload, and must be uploaded again |
| `JOB_NOT_FOUND`, `BATCH_NOT_FOUND` | control, read | 404 | no row of this tenant has the id or the batch key |
| `INVALID_QUERY` | read | 400 | a query parameter of a read is out of its range or not a known value |
| `TEMPLATE_NOT_ALLOWED` | control | 400 | a `template` sent with an action other than `resume`, or on a batch control call |
| `NOT_PAUSED` | control | 409 | a resume of a job that is not paused |
| `STOPPING` | control | 409 | a pause or resume while a cancel or terminate is outstanding |
| `PAUSE_IN_PROGRESS` | control | 409 | a resume while a pause is outstanding |
| `RESUME_PENDING` | control | 409 | a pause of a job at `resume-pending` |
| `ALREADY_ENDED` | control | 409 | a pause or resume of a job in a terminal status |
| `PATCH_PENDING` | control | 409 | a resume whose patch differs from the pending one |
| `PATCH_NOT_STARTED` | control | 409 | a resume with a patch of a job that never started (D18) |
| `PATCH_INVALID` | control | 400 | the validator refuses the patched template |
| `PATCH_INCOMPATIBLE` | control | 422 | `patch_check` refuses the patch, with its issues in `message` |
| `DATABASE_UNAVAILABLE`, `STORE_UNAVAILABLE`, `TEMPORAL_UNAVAILABLE` | any | 503 | a dependency the route needs did not answer, and nothing was written |

#### Control table

The state is the row's status, split by whether the job has a workflow (D4).
Every control call opens one transaction per job, a batch call one for each job of the batch (D19), reads the row with `lock_job` of E.3, decides on the locked row and writes in the same transaction, so admission's claim either committed before the lock and the row is at `admitted`, or waits and then skips the row.
"Request" is `request_action(job_id, action)`, every event is built by D5's rule and inserted in the same transaction as the row writes, and the retrier of D6 delivers every `requested` outcome except a resume, which admission delivers.

| State | `cancel` | `terminate` | `pause` | `resume` |
| --- | --- | --- | --- | --- |
| `accepted` | applied: request, row-only move to `cancelled`, events `job.cancel.requested` and `job.cancelled` | as `cancel`, with `terminated` and `job.terminate.requested` | applied: request, row-only move to `paused`, events `job.pause.requested` and `job.paused` | refused `NOT_PAUSED` |
| `admitted` or `running` | requested: request, event `job.cancel.requested`, view `cancelling` | requested, as `cancel` | requested: request, event `job.pause.requested`, view `pausing` | refused `NOT_PAUSED` |
| `paused`, no workflow | applied: request, move to `cancelled`, events `job.cancel.requested` and `job.cancelled` | applied, as `cancel` | noop | applied: request, move to `resume-pending`, row-only move to `accepted`, events `job.resume.requested` and `job.resumed`. With a patch: refused `PATCH_NOT_STARTED` |
| `paused`, workflow | requested, as for `running` | requested | noop | requested: request, move to `resume-pending`, `pending_patch_ref` set to the patch when one is sent and cleared otherwise (D18), event `job.resume.requested` |
| `resume-pending`, no workflow | applied: request, move to `cancelled`, events as above | applied | refused `RESUME_PENDING` | noop without a patch, refused `PATCH_NOT_STARTED` with one. The resume finisher of D6 makes the row-only move to `accepted` |
| `resume-pending`, workflow | requested | requested | refused `RESUME_PENDING` | noop without a patch or with a patch whose manifest hash equals the pending one, else refused `PATCH_PENDING` |
| terminal | noop | noop | refused `ALREADY_ENDED` | refused `ALREADY_ENDED` |

An action is outstanding while `requested_action` is not null, as the state contract defines it (`05-state-storage.md:73`): the move into `paused` clears a pause, the move from `resume-pending` to `running` or the row-only move to `accepted` clears a resume, and a move into a terminal status clears whatever remains.
So every applied cell ends with the move that clears its own request, and leaves nothing outstanding.

Overlay rules for an outstanding `requested_action`, applied before the table:

- The same action again is a noop that returns the outstanding `control_seq`, which is what `request_action` already does, except a `resume` whose patch differs from the pending one, which the `resume-pending` cells decide.
- `terminate` outstanding: `cancel` is a noop, and `pause` and `resume` are refused `STOPPING`.
- `cancel` outstanding: `terminate` takes over with the next sequence, and `pause` and `resume` are refused `STOPPING`.
- `pause` outstanding: `cancel` and `terminate` take over, and `resume` is refused `PAUSE_IN_PROGRESS`.
- `resume` outstanding: the row is at `resume-pending`, and the table applies.

Every move above is in `contracts/status/status.yaml:51-74`, every row-only move is one that the status contract marks `row_only`, and every event is one that `contracts/stages/stages.yaml:13-25` gives to the control stage.

Contract loader reasons, in check order: `schema-violation`, `too-large`, `unknown-reason` (an answer or error names a code outside `reasons.yaml`), `reason-scope` (a code used outside its scope), `transition-not-allowed` (a cell names a move outside the status contract, or a row-only move for a state with a workflow), `event-not-of-stage` (a cell names an event that the control stage does not emit), `control-cell-missing` (a (state, action) pair has no cell) and `fingerprint-mismatch` (a vector whose output differs from the formula).
Each schema has two valid files and at least one invalid file under every reason that applies to it.

### Rules agreed with epics that do not exist yet

These are stated so the owner epic confirms them in its own spec.
#7, the capacity revisit of #2 and #8 have since been built, so their rows say what was built and what this epic takes from it.

| Epic | Rule |
| --- | --- |
| #7 Broker | Built, closed on 2026-11-16. `wft_broker.client.connect(address, Credentials(user, password)) -> Connection`, whose `publish(routing_key, body, timeout_s=5.0)` takes the bytes of `wft_events.to_bytes` or of a stored event and returns `Outcome.ACCEPTED`, `REJECTED` (a full queue) or `UNROUTABLE` (no queue for the key), and raises `TopologyError` (the exchange is missing), `PublishTimeout` (no outcome in `timeout_s`, under an alarm) and `BrokerUnavailable` (the connection is lost, with no reconnect), all from `wft_broker.models`. `wft_broker.testing.FakeBroker` and `broker_contract`, `wft_broker.testing.container.broker_container`, and `wft_broker.tenancy.load(profile)` over `contracts/tenancy/tenancy.yaml`, whose `ci` profile holds the tenant `acme` and the class `standard` alone. The module `rabbitmq` with the Service `rabbitmq` in `wft-infra` on port 5672, and the password helper of the broker user `dispatch`. The dead-letter reasons of `contracts/broker/reasons.yaml`, recovered by an operator's `python -m wft_broker.dlq replay`, which never relies on #24 republishing a row that holds a receipt. This epic confirms #7's rules row for it (`10-broker.md:255`) in decisions 8, 22 and 23 and in D16: the publish signature with `timeout_s`, the handling of `PublishTimeout` and the reconnect, the address and the Secret hand-off, pending depth read from its own rows, a full queue answered as accepted, and no replay route |
| #8 Admission | Built on `main` [review] as `12-admission.md` states, with its receipt lane W still to land. Claims a row by `transition(accepted, admitted)` committed before the start, claims only a row at `accepted` whose `requested_action` is null, sets a missing receipt itself in the claim's transaction when it holds a delivered message for the row and then records `job.published` from `/wft/admission` (lane W), and treats a `Lost` answer as a skip. Re-admits a row at `accepted` with a receipt, `control_seq` above 0 and `requested_action` null, and a row at `admitted` with no workflow. Sends `resume` with the row's `control_seq` and `pending_patch_ref` while `requested_action` is `resume`, and is the only sender of `resume`. Reads `seed`, `parameters` and `job_class` from the row as submit wrote it and passes `class_override` null. Starts every job this API accepted with no `wft_pool_queue_prefix` memo, so it reaches the deployed pool workers on the contract queue names (#10's D22). Settles a delivery with `release()` only for #7's four causes (a dependency outage, a job class at its cap, its internal breaker open, its drain on shutdown), so a released job stays at `accepted` with its receipt and waits in its queue, which the reconciler never republishes (D7) |
| #13 Observability | Adds the metric names of D15 to `metrics.yaml`, owns the alerts `ReconcilerBacklogOld` and `ControlOutboxOld` that its body names, and exports the `wft_events.metrics` registry, with the broker client's `wft_broker_publish_total`, from this service as #7's rules row for #13 says |
| #14 Load CLI | Is a pure client of `contracts/api/`, and owns `reconcile` |
| #15 Failure scenarios | Drives the API for a kill during a batch, a Temporal outage during cancel and pause, a batch cancel of running jobs and a pause racing the last task |
| #16 Characterization | Replaces the estimates of D10, D6 and D7 and the module figures of D16 with measured values, using S7's numbers as the first data point, and measures the reconciler's publish rate against the client's one blocking publish at a time (decision 23), as #7's rules row for #16 asks |
| #17 Test ladder | Uses submit, status, cancel, pause and resume through this API in its core pass, and owns the canonical end-to-end cases (decision 11) |
| #18 Checkpoint | Proves a patched resume from a checkpoint end to end through `pending_patch_ref` as this epic writes it |
| #2 capacity revisit #366 | Landed (`c8fa830`): the 6,144 MiB infra node and a reserve row `dispatch` of 100m and 256 MiB, so the module takes the name `dispatch` (decision 24) and the budget check stops counting the reserve once D.2 lands the module |

## 1. Goal and non-goals

Goal: the front door of the platform, for submission and for operation.
A submitter sends a batch of up to 100,000 jobs in one call and gets a per-job answer, and every accepted row is durable when the answer ends.
An operator cancels, pauses, resumes (with an optional patch) or terminates a job, including a job that has not started.
The API writes the job row first and holds no job state of its own: Postgres owns the control record and Temporal owns lifecycle.
Every call is safe to repeat: the same key gives the same job, and the same control request has its effect once.

Deduplication sits at five points of the chain, each keyed on the job, and none of them stops a duplicate message.
The row's unique key `(tenant, submission_key)` makes a resent entry land on the same job.
The receipt `published_at` keeps the reconciler from republishing a row once a publish is known to have landed, and admission's claim sets it from a delivered message when dispatch has not (D7).
The broker deduplicates nothing, by design (#7's decision 10, `contracts/broker/README.md:62`).
Admission's claim is a compare-and-set that admits a job once and acknowledges and counts every later message for it.
The workflow id `workflow_id(job_id)` with `REJECT_DUPLICATE` starts one workflow per job.
So a message can be doubled and a job cannot, and "doubled" in this spec counts rows, events and workflow executions, never messages (B.3).
The RabbitMQ message-deduplication plugin was evaluated on 2026-11-18 and rejected:

- it refuses to run on RabbitMQ 4.4.0, the version #7 pins
- its queue-level mode silently does nothing on quorum queues, which the intake queues are
- its exchange-level mode needs the key as a message annotation, which the broker contract forbids
- it answers a dropped duplicate with `released`, which the client reads as `UNROUTABLE`, so in a crash between the publish and the receipt the reconciler's republish is dropped and the receipt is never marked
- it keeps the key of a publish that a full queue rejected, so the reconciler's retry is dropped until the key expires
- a dropped duplicate leaves no record tied to its job, where today every duplicate reaches admission and is counted

### Breakdown inputs updated for what earlier epics landed

The epic body was written on 2026-10-27 and touched last on 2026-11-03, before the state, artifacts, toolchain, workflow, broker and workers epics closed, and these changes apply.

| Epic body item | Now | Source |
| --- | --- | --- |
| Contract versions | events v4 (E.2, #429), job v2, artifact v3, store v3, state v2, status v1, stages v1, temporal v2 (pool table v0 with the 512 MiB simulate slot, signals and queries unchanged), instrumentation v2 (the `wft_broker_*` counters), preset v1, module v1, broker v1, tenancy v1, and after the early lanes store v4 and state v3, and after the receipt lane of #8 stages v2 and the next state version (E.3) | the `VERSION` files [built] [review] |
| A broker client, a tenant configuration and a dead-letter reason set to be agreed with #7 | Built by #7: the client, its fake and T1 container, the tenancy profile and the reason table, which this epic uses as they are | decisions 8, 22 and 23 |
| One service on one NodePort | Two reserved ports, `control-api` and `dispatch-api`, served by one process with two listeners | decision 18, D1 |
| The API signals `resume` | Admission signals `resume`, and the API sends `cancel`, `terminate` and `pause` only | decision 4 |
| Bundle resolution credited to #5 | The real `SourceResolver` is this epic's | decision 5 |
| Upload by hash with no job id | Upload stores the object unindexed, and submit indexes and links it to each job (D11), because `put_content` with a connection needs a job id | D11, `packages/artifactclient/src/wft_artifactclient/client.py:301-302` [read] |
| The `submit` store identity reads `wft-sources` | It can only write there today, and E.1 grants `get` and `list` | decision 6 |
| Events to emit: accepted and the cancel, pause and resume requested events | Also `job.published`, `job.terminate.requested` and the row-only `job.paused`, `job.resumed`, `job.cancelled` and `job.terminated` of a job that never started | decision 19 |
| The control transition table "owned by #3" | `wft_jobstate` guards moves but `request_action` checks no status and no call writes `pending_patch_ref`, so this epic owns the control table (P.1) and E.3 adds the patch writer to the state library | decision 20, `packages/jobstate/src/wft_jobstate/jobs.py:189-229` [read] |
| Dead-letter reasons `MALFORMED`, `INVALID_DAG`, `UNKNOWN_TENANT`, `UNSUPPORTED_VERSION` | `INVALID_DAG` dropped, `UNKNOWN_TENANT` an API reason only | decision 8 |
| Priority classes `high`, `normal`, `low` in neighbouring bodies | `standard` only for the proof of concept | decision 9 |
| A generated key when no key is given | No generated key: the batch key is required | D9 |
| Manual pause of a running task, steps 1 to 5 | Built by the workflow epic (decision 7 of `08-workflow-engine.md:38`) and on the worker side by #10, which withdraws a running simulate task whose plan sets `checkpoint` with no checkpoint until #18, while every other running task runs to completion, and the API only requests the pause and signals it | decision 4, decision 25, `08-workflow-engine.md:38` [read], `11-workers.md:71` [built] |
| Open questions 3, 5, 9, 11 | Decided | decisions 13 to 15, D1 |
| Open question 4 (operator-only terminate and patch), 6 (numbers and a pending cap), 7 (unknown tenant), 8 (resume order of a job paused before start), 10 (admission depth) | Q3; D10 and Q4; decision 8; decision 10 makes it admission's; `GET T/depth` counts rows at `accepted` | 0.2, 0.3 |
| Done means levels 1 to 4 green and `reconcile` of #14 | Rewritten as what this epic can prove | decision 12 |
| The intake diagram as the epic's diagram | Kept, and the submit and control paths are drawn in 0.2 (issue) as the epic's detail issue 14 asks | 0.2 (issue) |

### Delivered already, and what is left

| Epic body item | State on `main` | Where it is, or what is left |
| --- | --- | --- |
| The job row, the unique key, the control columns, the guarded move and the grants | Delivered by #3 | `contracts/state/`, `packages/jobstate/`, `R__roles.sql` |
| Event models, ids, `build_event`, key formulas, the stage table | Delivered by #4 | `packages/events/`, `packages/keys/`, `contracts/stages/` |
| Validation, plan compile, job params check, patch check | Delivered by #5 | `packages/jobcheck/`, `packages/plan/`, `packages/jobpatch/` |
| Content storage, index and link | Delivered by #12 | `packages/artifactclient/`, `packages/artifactindex/` |
| Bundle reader | Delivered by #11 | `wft_compile.bundle.info` |
| Signal and query models, the workflow's handling of every control signal | Delivered by #6 | `contracts/temporal/`, `wft_workflow.models`, `JobWorkflow` |
| Broker client, fake broker, T1 container, tenancy profile and loader, the `rabbitmq` module | Delivered by #7 | `packages/broker/` (`wft_broker`), `contracts/broker/`, `contracts/tenancy/`, `deploy/rabbitmq/` |
| Pool workers that run a job's tasks, and the withdrawal of a running task on cancel | Delivered by #10 | `packages/worker/`, `deploy/workers/` |
| Job class vocabulary on events | Delivered by E.2 of this epic, #429 (`7959b5c`) [review] | `contracts/events/` at events v4 |
| Store role change, patch writer | Left, as early lanes | E.1, E.3 |
| API contract, package, stubs | Left | 0.1 (issue), 0.2 (issue) |
| Submit, uploads, resolver, publish, reconciler | Left | P.2, B.1, B.2, B.3 |
| Control, retrier, patch, reads | Left | P.1, C.1, C.2, C.3, R.1 |
| Service and deployment | Left | D.1, D.2 |

Non-goals for this pass:

- everything the epic lists under "Does not own": the job table and its guarded move (#3), intake queues, the dead-letter queue and its tooling (#7), the admission policy, the start call and the resume signal (#8), validation and patch rules (#5), event schemas (#4), the artifact client and store (#12, #3), signal handlers, drain and the paused state (#6), the worker side of a cancel or a pause (#10), the checkpoint contract, the checkpoint written on an interrupt and `operator_interrupt` (#18), the load client (#14), dashboards and alerts (#13), the suites above integration level (#17), an authentication product, a web UI, per-tenant rate limits and several replicas
- the tenant and priority-class configuration, which the broker epic pinned as tenancy v1 (D17)
- the recoverable signal of a dead letter that no event names, and replay, which are #7's `dlq` tool for an operator (decision 8)
- an OpenAPI document (D2)
- measuring the API at 100,000 jobs per call against the cluster, which is #16's run, with B.2's 100,000-job call and S7 as this epic's T1 data points

## 2. Interfaces

### Consumes (each pinned to a version)

| From | What | Pinned at | Stub used until it exists |
| --- | --- | --- | --- |
| Dev environment | Dev shell, `just test`, `just itest`, `just ci`, `just up`, `just down`, `just nuke`, `wft_itest` | `flake.lock`, `uv.lock` | none, exists [read] |
| Cluster | Preset `ci`, module contract, namespaces, host ports `control-api` and `dispatch-api`, cluster lock | preset v1, module v1, `deploy/host/ports.yaml` | none, exists |
| Capacity revisit [#366](https://github.com/AndoNorth/weft/issues/366) | The 6,144 MiB `ci` infra node, the reserve row `dispatch` of 100m and 256 MiB, and 502 MiB free on that node with the `rabbitmq` module counted | landed, `c8fa830`, `scripts/cluster/budget.sh:35-39` [built] | none, exists |
| Envelope | Event models, `build_event`, `to_bytes`, `wft_keys`, status table, stage table and `run_stage_contract`, `wft_temporal` converter and tracing interceptor | events v4, keys v1, status v1, stages v1 (v2 after the receipt lane of #8) | none, exists |
| Instrumentation | Metric, label and log rules, and the `wft_events.metrics` registry that counts the broker client's publishes | instrumentation v2 | none, exists |
| Validator | `wft_jobcheck.validate`, `SourceResolver`, `BundleInfo`, `BundleNotFound`, `resolver_contract`, `wft_plan.compile_plan`, `canonical_bytes`, `check_job_params`, `read_plan`, `wft_jobpatch.patch_check`, the `ci` limits profile | job v2, validator v1 | none, exists |
| State | `insert_accepted`, `get_job`, `transition`, `request_action`, `mark_published`, `mark_delivered`, `insert_events`, `events_for_job`, and after E.3 `lock_job`, `set_pending_patch` and the receipt time from `mark_published`, the roles `wft_dispatch`, `wft_control`, `wft_artifacts` and `wft_reader` | state v2 (v3 after E.3), store v3 (v4 after E.1) | none, exists |
| Artifacts | `put_content`, `get`, `head`, `index_reference`, `link_use`, the staging kind caps and the layout kinds | artifact v3 | none, exists |
| Toolchain | `wft_compile.bundle.info` | compile v1 | none, exists |
| Workflow | `ControlSignal`, `StatusQuery`, signal and query names of `naming.yaml`, `workflow_id`, and for tests `JobWorkflow`, `FakePool` and `FakeControl` | temporal v2, workflow v1 | none, exists |
| Broker (#7) | `wft_broker.client` (`connect`, `Credentials`, `Connection.publish`), `Outcome`, `TopologyError`, `PublishTimeout` and `BrokerUnavailable` of `wft_broker.models`, `wft_broker.contract.topology()` for the routing key template, `wft_broker.tenancy.load`, `wft_broker.testing.FakeBroker`, `wft_broker.testing.container.broker_container`, `images.rabbitmq` 4.4.0 for T1, the `rabbitmq` module and its password helper for T2 | broker v1, tenancy v1, closed 2026-11-16 [built] | none, exists (decision 22) |
| Workers (#10) | The pool Deployments `compile-worker`, `simulate-worker` and `aggregate-worker` in `wft-workers`, one slot each (compile 1,000m and 1,024 MiB, simulate 250m and 512 MiB, aggregate 1,000m and 1,024 MiB, as pool table v0 of temporal v2 holds), polling the unversioned queues `wft-compile`, `wft-simulate` and `wft-aggregate`, for the T2 rows of the exit test | module v1, temporal v2, closed 2026-11-16 [built] | none, exists (decision 25) |
| Admission (#8) | The claim, the re-admission scan, the resume signal, and the `admission` module for T2 | built on `main` [review] (`packages/admission/`, `deploy/admission/`), with its receipt lane W still to land and the epic open until its exit test | T0 and T1 tests claim with `transition(accepted, admitted)` and send `resume` themselves, as the workflow harness does, so this package takes no dependency on `wft_admission`, and the T2 rows run the real module |

No `revisit` child issue of an earlier epic blocks this epic: #366 has landed.
This spec adds one: E.3, a child of #3 that adds the patch writer, the row lock and the receipt winner to the state library.
E.1 is a contract-change PR on the store contract and needs no child issue, and E.2 was one on the events contract, delivered by #429.
The names below were checked free on `main` [built: no `packages/dispatchapi/`, no `contracts/api/`, no `deploy/dispatchapi/`, no `module.yaml` named `dispatch`, no `docs/architecture/*dispatch-and-control*`, no `docs/delivery/spikes/S7/`].

### Produces (each pinned precisely)

| To | What | Where it lives (path) | Shape | Version |
| --- | --- | --- | --- | --- |
| #14, #17, #15, #16 | API contract | `contracts/api/` | `routes.yaml`, `reasons.yaml`, `control.yaml`, the model schemas, `vectors/fingerprint.json`, loader and reasons as 0.4 | api v1 |
| #8, #6 | Accepted rows, accepted and published events, intake messages, control requests and their signals | the `accepted_job` and `event` tables, the intake queues | the state contract, events v4, `ControlSignal` of temporal v2, the intake message of broker v1 | state v3, events v4 |
| #8, #6, #18 | `pending_patch_ref` | the job row | a `ManifestReference` or null, written by `set_pending_patch` of E.3 | state v3 |
| every epic that uploads or reads `wft-sources` | Store role `submit` with `get` and `list` | `contracts/store/roles.yaml` | E.1 | store v4 |
| every event producer and reader | Job class vocabulary on the event attribute | `contracts/events/` | E.2, checked at the API per Q1 | events v4 |
| #17, #14, #15, #16 | The service and its module | `packages/dispatchapi/` (`wft_dispatchapi`), `deploy/dispatchapi/` | `python -m wft_dispatchapi.service`, module `dispatch` in `wft-control`, NodePorts 30081 and 30080 | dispatchapi v1 |
| #13 | Metrics | the service's port 9090 | the names of D15 | dispatchapi v1 |
| #16 | Spike result | `docs/delivery/spikes/S7/batch-submit.md` | pass or fail, numbers, decision | none |

### Assumptions about epics drafted at the same time

The breakdown specs of #7, #8 and #10 were drafted beside this one, and these are the assumptions this spec made about them, for the review to check.
#7 and #10 have since closed, so each of their rows says whether it held against what they built [built], and a row that changed names the decision that takes the built shape.

| # | Epic | Assumption | Where this spec depends on it |
| --- | --- | --- | --- |
| A1 | #7 | Changed. Built as `wft_broker.client.connect(address, Credentials(user, password))` and `Connection.publish(routing_key, body, timeout_s=5.0) -> Outcome`, with `Outcome.ACCEPTED`, `REJECTED` or `UNROUTABLE`, `TopologyError` on a refused link attach, which stops publishing and is never retried, `BrokerUnavailable` on a lost connection with no reconnect inside the library, and `PublishTimeout`, which the draft lacked, when an alarm withholds the outcome. A `Connection` may be shared between threads, and its publishes run one at a time. The `dispatch` broker user's password comes from #7's helper, and the image builds proton 0.40.0 from its sdist (decisions 22 and 23, D16) | B.3, D16, D7, D8 |
| A2 | #7 | Held. The client publishes to the address `/exchanges/wft.intake/<routing key>`, and the routing key is the intake queue's own name, the template `intake.<tenant>.<class>` of `contracts/broker/topology.yaml:8`, which the API fills from the row and reads through `wft_broker.contract.topology()` rather than spelling it. A key outside the broker topology gives `UNROUTABLE`, not `TopologyError` | B.3 |
| A3 | #7 | Held. `contracts/tenancy/tenancy.yaml` holds one profile per preset (`ci`, `dev`, `soak`), read through package data by `wft_broker.tenancy.load(profile)`, and the `ci` profile holds the tenant `acme` and the classes `[standard]`. The profile also holds `reserve_interval`, `caps.open`, `caps.global`, `caps.job_class` and per-tenant `quota`, which admission reads; this epic reads only `tenants` and `priority_classes`, needs no field the profile lacks, and asks #7 for nothing | D17 |
| A4 | #7 | Held. The broker module is `rabbitmq` in `wft-infra` with the Service `rabbitmq` on port 5672, and the T1 image is `images.rabbitmq` 4.4.0 by digest in `deploy/versions.yaml`, started by `wft_broker.testing.container.broker_container`, not by `wft_broker.testing.broker_container` as the draft named it | B.3 T1 rows, D16, section 7 |
| A5 | #7, #8 | Held. Under #7's Q1 (a) an operator recovers a dead letter that no event names with `dlq replay`, which needs nothing from this epic, and the reconciler's rule of D7 (never republish a row with a receipt) holds (decision 8) | D7, Q2 |
| A6 | #8 | Held [review]: admission commits `admitted` before the start (`packages/admission/src/wft_admission/claim.py:85-106`), re-admits by the rule that the rules row for #8 states (`12-admission.md` D10), and alone sends `resume` | D4, D6, C.1, C.2 |
| A7 | #8 | The admission module is named `admission` and is deployed before this epic's exit test | section 7 |
| A8 | #10 | Held. The real pool workers are on `main` (#10 closed on 2026-11-16), so rows 12 and 13 of the exit test stay here. They poll the contract queue names with no prefix, a job of this API reaches them only when admission starts it with no `wft_pool_queue_prefix` memo, and a pause withdraws only a running simulate task whose plan sets `checkpoint` (decision 25) | section 7, D.2 |
| A9 | #9 | Placement changes nothing this epic calls | none |
| A10 | #2 | Changed. #366 named the reserve row `dispatch`, so the module of D16 takes the name `dispatch` (decision 24) | D16, D.2 |

## 3. Lanes

Package and module names are proposals.
Lane 0 owns every file it creates until the owning lane takes over the glob, except the contract modules of `wft_dispatchapi`, `wft_dispatchapi.ports` and `wft_dispatchapi.testing`, which stay lane 0's.
`wft_dispatchapi` below stands for `packages/dispatchapi/src/wft_dispatchapi` and `tests/` for `packages/dispatchapi/tests`.

Departures from the method, stated so the review approves them knowingly:

- Lane E runs beside lane 0, because its three issues change closed epics' contracts and libraries and touch no file of lane 0.
  E.1 is a contract-change PR on the store contract, and E.2 was one on the events contract, delivered by #429.
  E.3 is filed as a `revisit` child of #3, and lands as two PRs: first a contract-change PR that touches `contracts/` only, then the code.
- Spike S7 runs beside lane 0, because it writes only under `docs/delivery/spikes/` and gates nothing.
- Lane 0 carries no `just/*.just` module and adds no recipe: tests run through `just test`, `just itest` and `just ci`, and the service and the smoke driver are `python -m` commands, as the toolchain and workflow epics recorded.
- Lane Needs run across lanes where a test over a stub would prove only the stub: the uploads and the resolver need the store role (B.1 after E.1), the publisher needs the receipt winner (B.3 after E.3), control needs the row lock (C.1 after E.3), the patched resume needs the patch writer and the resolver (C.3 after E.3 and B.1), the service wires every route (D.1 after B.3, C.2, C.3 and R.1), and the deployment needs the admission module (D.2 after the module of #8), since the capacity revisit, the `rabbitmq` module and the pool workers are on `main`.

| Lane | Workers | Owns (exclusive) | Issues | Needs |
| --- | --- | --- | --- | --- |
| E Early changes to closed epics | 2 (one per open issue, E.2 delivered) | E.1: `contracts/store/{roles.yaml,VERSION,README.md}`, the `store` row of `docs/contracts.md`, and the store contract's consumer edges in `docs/architecture/15-contracts.drawio` and its svg. E.2: delivered by #429, so it owns nothing now. E.3: `contracts/state/{README.md,VERSION}`, `packages/jobstate/src/wft_jobstate/{__init__,jobs}.py`, `packages/jobstate/tests/test_{pending_patch,lock_job,mark_published}.py`, and one closure addendum line of `docs/delivery/epics/05-state-storage.md` | E.1, E.2, E.3 | nothing, beside lane 0 |
| 0 Contract and skeleton | 1 | `contracts/api/**`, `packages/dispatchapi/{pyproject.toml,README.md}`, `wft_dispatchapi/{__init__,models,contract,gen,ports}.py`, the package data under `wft_dispatchapi/contract/**`, `wft_dispatchapi/testing/**`, `tests/{conftest,test_loader,test_drift,test_fingerprint_vectors,test_stub_contracts,test_wheel}.py`, the two `wft-dispatchapi` lines of the root `pyproject.toml`, the `wft-dispatchapi` entry of `uv.lock`, the `api` row of `docs/contracts.md`, the `api` contract in `docs/architecture/15-contracts.drawio` and its svg, `docs/architecture/NN-dispatch-and-control.drawio` and its svg | 0.1, 0.2 | nothing (barrier, merged alone) |
| S Spike | 1 | `docs/delivery/spikes/S7/**`, the S7 row of `docs/delivery/spikes/README.md` | S7 | nothing, beside lane 0 |
| P Pure rules | 2 (one per issue) | `wft_dispatchapi/{control_table,keys,ndjson,limits}.py`, `tests/test_{control_table,keys,ndjson,limits}.py` | P.1, P.2 | lane 0 |
| B Submit path | 1 | `wft_dispatchapi/{objects,resolver,submit,publish,reconciler}.py`, `tests/{objects,submit,publish}/**` | B.1, B.2, B.3 | lane 0, E.1 for B.1, B.1 and P.2 for B.2, B.2 and E.3 for B.3 |
| C Control | 1 | `wft_dispatchapi/{control,retrier,gateway,patch}.py`, `tests/control/**` | C.1, C.2, C.3 | lane 0, P.1 and E.3 for C.1, C.1 for C.2, C.2, E.3 and B.1 for C.3 |
| R Reads | 1 | `wft_dispatchapi/reads.py`, `tests/reads/**` | R.1 | lane 0 |
| D Service and deployment | 1 | `wft_dispatchapi/{service,smoke}/**`, `tests/service/**`, `deploy/dispatchapi/**` (module `dispatch`, its image, the login job of D13, the store Secret and the broker Secret), the `deploy/dispatchapi` entry of `TREES` in `packages/migrate/tests/test_credentials.py`, and the `dispatch` entry of the reserve loop of `test_ci_infra_node_figures` in `scripts/cluster/test_budget.py` | D.1, D.2 | B.3, C.2, C.3 and R.1 for D.1, D.1, the module of #8 and W.1 of `12-admission.md` for D.2 |

Shared-file rules:

- `uv.lock` is the one hotspot, and lane 0 changes it by the `wft-dispatchapi` entry.
  Lane 0 declares in `packages/dispatchapi/pyproject.toml` every dependency the lanes need (the workspace packages of the Consumes table, `wft-broker` among them, and the locked `temporalio`, `psycopg`, `pydantic`, `pyyaml` and `boto3`), so no other lane edits it.
  A lock conflict is never resolved by hand, and `uv lock` is rerun after rebase.
- The root `pyproject.toml` changes only in 0.1 (issue), by one `wft-dispatchapi` line in `[tool.uv.sources]` and one in the dev dependency group.
- `routes.yaml` names no handler, so it holds no internal function name in a contract that #14 consumes.
  D.1 holds the route table as a literal map from route to handler, and `test_route_table_equals_routes_yaml` holds its keys equal to `routes.yaml`, the pattern P.1 uses for `control.yaml`.
- `docs/architecture/15-contracts.drawio` and its svg are edited by 0.1 (issue), which adds the `api` contract, and by E.1, which adds #24 as a consumer of the store contract, and the two run side by side, so whichever lands second rebases onto the first and exports the diagram again.
- `contracts/state/{README.md,VERSION}` are edited by E.3 and by the receipt lane W of `12-admission.md`, which adds admission's receipt write to the writers table, so whichever lands second rebases onto the first and takes the next version, and the `state v3` of this spec means the version that E.3 produces.
- `wft_dispatchapi/__init__.py` and `tests/conftest.py` are lane 0's, and a lane imports by module path.
  Lane 0's conftest holds the fixtures that several lanes share (Postgres, SeaweedFS and the Temporal dev server through `wft_itest`, the `ci` tenancy profile from `wft_broker.tenancy.load`, `FakeBroker`, and the four logins of D13 created on the test database), and a lane keeps its own fixtures in a `conftest.py` inside its own test directory.
- No pytest session holds more than two containers, and a test that needs two at once carries `pair`, because the `t1` fixture of `wft_itest` sizes its session by that marker.
  Lane B and the store tests of C.3 hold Postgres with SeaweedFS, lane C and D.1 hold Postgres with the Temporal dev server, lane R holds Postgres alone, and the B.3 RabbitMQ tests hold Postgres with RabbitMQ through `wft_broker.testing.container.broker_container`.
- `contracts/api/README.md` is lane 0's, and after lane 0 merges a pinned file changes only through a contract-change PR.
- `docs/architecture/NN-dispatch-and-control.drawio` and its svg are drawn by 0.2 (issue) before any other lane starts, and the exit-test worker re-checks them at closure.
- `docs/delivery/spikes/README.md` gets one appended row for S7.
- `packages/migrate/tests/test_credentials.py` changes only in D.2, by adding `deploy/dispatchapi` to `TREES`.
- `scripts/cluster/test_budget.py` changes only in D.2, by moving `dispatch` from the reserves that `test_ci_infra_node_figures` expects to a module line (decision 24). #8's module edits the same line for `admission`, so whichever lands second rebases onto the first.
- `justfile`, `just/*.just`, `.github/**`, `nix/**`, every other file of `scripts/**`, `docs/dev-setup.md`, `deploy/host/ports.yaml`, `deploy/versions.yaml`, `contracts/instrumentation/**`, `packages/workflow/corpus/**`, every other package except the edits of lane E and the one `TREES` edit, and every other `deploy/` directory, `deploy/rabbitmq/` and `deploy/workers/` included, are not touched.
  The image pins `python` as the workflow module does, so `deploy/versions.yaml` needs no entry.
- Generated schemas are committed, regenerated by `python -m wft_dispatchapi.gen`, and never hand edited.
- No tracked file holds a credential: passwords and store credentials come from the state directory, as the workflow module does.

## 4. Issues

One block per issue, in the [lane issue template](../../../.github/ISSUE_TEMPLATE/lane-issue.md), replaced by `#NNN` links after filing.
Done means, for every issue: `just ci` is green on the branch, the output of the named tests is pasted in the PR, and nothing outside Owns and the shared files changed.
An issue that names a T2 command runs it under the cluster lock and pastes its output.

### Mapping from the epic's detail issues

The epic numbers its list with two 4s and two 5s, so the second of each is named by its subject.

| Epic detail issue | Delivered by |
| --- | --- |
| 1 API skeleton and contract | 0.1 (issue) (routes, error body, reason codes), B.2 (the tenant check of D17 over #7's loader), D.1 (configuration and health), the OpenAPI document dropped (D2) |
| 2 Batch submit stream | P.2, B.2 |
| 3 Idempotency | P.2 (keys and fingerprint), B.2 (the duplicate and `KEY_CONFLICT` matrix), B.3 (crash and resend) |
| 4 Submit sequence | B.2, B.3 |
| 5 Reconciler | B.3 |
| 4 (second) Manifest and bundle upload | B.1, B.2 (the inline manifest of D12) |
| 5 (second) Accepted and control events | B.2, C.1 (ids by D5) |
| 6 Control endpoints | P.1, C.1 |
| 7 Control outbox and retrier | C.2 |
| 8 Resume with patch | E.3, C.3 |
| 9 Manual pause | C.1 and C.2 (request and signal only, decision 4), the rest built by #6 |
| 10 Status and batch reads | R.1 |
| 11 Metrics, traces and alerts | D.1 (metrics and traces), the alerts to #13 (D15) |
| 12 Deployment | D.2 |
| 13 Contract tests for #14 and acceptance cases for #17 | 0.1 (issue) (the contract and its vectors), the acceptance cases to #17 (decision 11) |
| 14 Diagram of the submit and control paths | 0.2 (issue) |
| Not in the list: the real `SourceResolver`, the store role, the job class vocabulary | B.1, E.1, E.2 |

### E.1 The submit identity reads `wft-sources`

Lane E, tier T0, and T1 for the role matrix. Needs: nothing. Blocks: B.1.

- Scope: the contract-change PR of decision 6, labelled `contract-change`, with a comment listing #24, #12 and #17 as consumers.
  It adds #24 to the consumers of the store contract in `contracts/store/README.md`, in the `store` row of `docs/contracts.md` and in `docs/architecture/15-contracts.drawio`, because a change of consumers updates both in the same PR (`docs/delivery/method.md:102`).
- Owns: lane E's E.1 globs.
- Consumes / produces: store v3.
  Produces store v4, where `submit` holds `get`, `list`, `put` and `multipart` on `wft-sources` and nothing elsewhere.
- Acceptance criteria:
  - [ ] `contracts/store/check.sh contracts/store/roles.yaml` exits 0, and `pytest packages/storecheck -v -k "roles or loader or provision"` passes unchanged.
  - [ ] `pytest packages/storecheck -v -k role_matrix_equals_the_contract` passes against SeaweedFS, showing `submit` reading and listing `wft-sources` and still refused a write to `wft-results`.
  - [ ] `pytest packages/storecheck -v -k only_the_reaper_deletes` passes, so `submit` gained no delete.
  - [ ] `scripts/check-contracts-map.sh` and `scripts/export-diagrams.sh --check` exit 0, and the diagram shows #24 consuming the store contract.
- Verifier: `packages/storecheck/tests/test_roles.py`, `test_store.py`.

### E.2 Job class vocabulary on events

Lane E, tier T0. Delivered by #429 (`7959b5c`, events v4) [review], so this block is the record and is not filed again. Blocks: nothing.

- Scope: decision 7 under the answer to Q1, as one contract-change PR touching `contracts/events/` only, with a comment listing every epic that builds events.
  The README states that `jobclass` takes a class of `classes` in `contracts/job/limits.yaml`, checked by the dispatch API where the class enters, and that readers accept any class name so recorded histories keep decoding.
  Every vector whose `jobclass` is `simulate` takes `short`, the golden files and `MANIFEST.sha256` are regenerated by `python -m wft_compat.vectors`, and `VERSION` is 4.
  `wft_events`, its profile and every recorded corpus are unchanged.
- Owns: lane E's E.2 globs.
- Consumes / produces: events v3, job v2.
  Produces events v4.
- Acceptance criteria:
  - [ ] `python -m wft_compat.vectors --check` and `python -m wft_events.schemagen --check` exit 0, and `git grep -lE '"jobclass": *"simulate"' contracts/events` prints nothing.
  - [ ] `pytest packages/compat packages/events packages/broker -q -rs` passes with no `SKIPPED` line, `packages/broker` because #7's suite, dead-letter tool and their tests publish the golden events.
  - [ ] `pytest packages/temporal packages/workflow -v -k replay` passes with `packages/workflow/corpus/v1/` byte-unchanged.
- Verifier: `packages/compat/tests/test_golden.py`, `packages/events/tests/test_contract_vectors.py`, the replay tests.

### E.3 State library additions for control and publish

Lane E, tier T1. Needs: nothing. Blocks: C.1, C.3, B.3.

- Scope: filed as a `revisit` child of #3 that links B.3, C.1 and C.3 of this epic as the lanes it blocks, lists #24 and #8 as the consumer epics affected (#8's tests seed receipts with `mark_published`), names the tests it touches, the three new files and every existing test that calls `mark_published` (`packages/jobstate/tests/`, `packages/stateops/tests/`, `packages/admission/tests/`), and adds a dated Closure addendum line to `05-state-storage.md` (`docs/delivery/method.md:132-147`).
  The contract-change PR documents three calls in `contracts/state/README.md` and bumps `VERSION` to 3, or to the next version when the receipt lane of #8 lands first (section 3), and the code PR adds them to `wft_jobstate`.
  `set_pending_patch(conn, job_id, control_seq, ref)`, under `wft_control`, is a compare-and-set that writes `pending_patch_ref`, or clears it when `ref` is null, only while `requested_action` is `resume` and `control_seq` equals the given sequence, and returns `Lost` otherwise.
  `lock_job(conn, job_id) -> JobRow | None`, under `wft_control`, reads the row `FOR UPDATE` inside the caller's transaction, so a control decision and its writes cannot interleave with admission's claim or with another control request.
  `mark_published` returns the receipt time when this call set it and `None` when a receipt was already there, so only the winner records `job.published`.
- Owns: lane E's E.3 globs.
- Consumes / produces: state v2.
  Produces state v3.
- Acceptance criteria:
  - [ ] `pytest packages/jobstate -v -k "pending_patch or lock_job or mark_published"` passes against Postgres: the patch write lands under the outstanding resume, clears with null, loses to a newer sequence and to a cleared action, and is refused as a member of `wft_dispatch`; a claim by `transition(accepted, admitted)` waits for a held `lock_job` and then sees the committed status; and of two concurrent `mark_published` calls exactly one returns a time.
  - [ ] `pytest packages/jobstate -q -rs` passes with no `SKIPPED` line, so every existing caller of `mark_published` still passes.
  - [ ] `python -m wft_jobstate.gen --check` exits 0.
- Verifier: `packages/jobstate/tests/test_pending_patch.py`, `test_lock_job.py`, `test_mark_published.py`.

### 0.1 API contract

Lane 0, tier T0. Needs: nothing. Blocks: every other issue but lane E and S7.

- Scope: the contract of 0.4 with its models, loader, generator, vectors and README, the `api` row of `docs/contracts.md`, and the contracts diagram.
- Owns: `contracts/api/**`, the `api` row of `docs/contracts.md`, `docs/architecture/15-contracts.drawio` and its svg, `packages/dispatchapi/{pyproject.toml,README.md}`, `wft_dispatchapi/{__init__,models,contract,gen}.py`, the package data under `wft_dispatchapi/contract/`, `tests/{conftest,test_loader,test_drift,test_fingerprint_vectors}.py`, the root `pyproject.toml` lines and the `uv.lock` entry.
- Consumes / produces: status v1, stages v1, temporal v2, job v2, read through package data.
  Produces api v1.
- Acceptance criteria:
  - [ ] `contracts/api/check.sh` exits 0 for every `vectors/*/valid/*` file and for `routes.yaml`, `reasons.yaml` and `control.yaml`, and 1 with the `reason:` of its directory for every `invalid` one, shown by `pytest packages/dispatchapi -v -k loader`.
  - [ ] `test_control_cells_are_status_moves` passes on the pinned `control.yaml` and fails on the vectors under `invalid/transition-not-allowed/` and `invalid/event-not-of-stage/`.
  - [ ] `test_fingerprint_vectors` passes, and fails after one input key is reordered in the formula.
  - [ ] `python -m wft_dispatchapi.gen --check` exits 0, and exits 1 after a model field is renamed.
  - [ ] `scripts/check-contracts-map.sh` and `scripts/export-diagrams.sh --check` exit 0.
- Verifier: `tests/test_loader.py`, `test_drift.py`, `test_fingerprint_vectors.py`.

### 0.2 Package skeleton, ports and stubs

Lane 0, tier T0. Needs: 0.1 (issue). Blocks: every code issue.

- Scope: the `WorkflowGateway` port in `wft_dispatchapi.ports`, the stub and conformance test of section 5, the wheel test, the shared fixtures, and the submit and control path diagram. The broker and the tenancy profile need no port or stub here (decision 22).
- Owns: the rest of lane 0's globs.
- Consumes / produces: the temporal contract's signal and query models, broker v1 and tenancy v1 through `wft_broker`.
  Produces the stub of section 5.
- Acceptance criteria:
  - [ ] `pytest packages/dispatchapi -v -k gateway_contract` passes against `FakeGateway`, and fails against a gateway that sends `resume`.
  - [ ] `pytest packages/dispatchapi -v -k wheel` builds the wheel, loads the contract from package data outside the repository, and loads the `ci` tenancy profile through `wft_broker.tenancy.load` in the same environment.
  - [ ] `scripts/export-diagrams.sh --check` exits 0, and the new diagram shows the two listeners, the submit sequence of D8, the reconciler, the control table, the retrier and the hand-off of `resume` to admission.
- Verifier: `tests/test_stub_contracts.py`, `test_wheel.py`.

### S7 One call of 100,000 jobs at T1

Lane S, spike, timebox 1.5 days, tier T1, throwaway code in `docs/delivery/spikes/S7/code/`. Needs: nothing. Blocks: nothing, and its numbers feed D10 and #16.

- Question: how long does the submit sequence of D8 take for 100,000 jobs on this host, with `wft_jobstate.insert_accepted`, `wft_eventstore.insert_events`, `index_reference` once and `link_use` per job, in chunks of 500, 1,000 and 2,000, against the `wft_itest` Postgres?
- Pass line: one chunk size finishes 100,000 rows, events and links with no chunk transaction longer than 5 s, and with a per-call memory peak, above the process baseline, of at most 24 MiB, so the 4 calls per process of D10 add at most 96 MiB to the service baseline that D.1 measures under the 256 MiB of D16.
- The result records rows per second, events per second, links per second, the longest chunk transaction and the peak memory per chunk size, and names the chunk size that D10 takes.
- On fail: D10 takes the chunk size with the shortest longest transaction, and the limit of 100,000 entries per call is raised with #16 as a risk to measure on the cluster.
- Result: `docs/delivery/spikes/S7/batch-submit.md`.

### P.1 Control table as a function

Lane P, tier T0. Needs: lane 0. Blocks: C.1.

- Scope: `decide(state, has_workflow, requested_action, action, patch) -> Decision`, the cells and overlay rules of 0.4 as a pure function, and the view derivation of decision 13.
- Owns: `wft_dispatchapi/control_table.py`, `tests/test_control_table.py`.
- Consumes / produces: `control.yaml`, `contracts/status/status.yaml`.
  Produces the decision function for C.1.
- Acceptance criteria:
  - [ ] `pytest packages/dispatchapi -v -k control_table` passes, including `test_function_equals_control_yaml` over every (state, workflow, outstanding action, action) combination, `test_nothing_leaves_a_terminal_status`, `test_repeat_is_a_noop` and `test_row_only_only_without_a_workflow`, each a property test over the full product.
  - [ ] `test_function_equals_control_yaml` fails after one cell of `control.yaml` is changed in a copy.
- Verifier: `tests/test_control_table.py`.

### P.2 Keys, fingerprint, stream reader and limits

Lane P, tier T0. Needs: lane 0. Blocks: B.2.

- Scope: the keys and fingerprint of D9, the NDJSON reader and writer, and the limits of D10 as configuration with the defaults.
- Owns: `wft_dispatchapi/{keys,ndjson,limits}.py`, `tests/test_{keys,ndjson,limits}.py`.
- Consumes / produces: `wft_keys`, `vectors/fingerprint.json`.
  Produces the reader and the key functions for B.2.
- Acceptance criteria:
  - [ ] `pytest packages/dispatchapi -v -k "keys or ndjson or limits"` passes, including `test_fingerprint_matches_vectors`, `test_job_key_defaults_to_batch_key_and_index`, `test_header_too_large`, `test_entry_too_large_is_an_entry_result`, `test_truncated_last_line_is_malformed`, `test_too_many_entries_answers_every_earlier_entry` and `test_idle_timeout`.
  - [ ] A property test shows the reader returns every line of a stream split at arbitrary byte boundaries.
- Verifier: `tests/test_keys.py`, `test_ndjson.py`, `test_limits.py`.

### B.1 Uploads and the real resolver

Lane B, tier T1 (SeaweedFS through `wft_itest`). Needs: lane 0, E.1. Blocks: B.2, C.3.

- Scope: the upload route of D11, and `StoreSourceResolver`, a `SourceResolver` over `wft-sources` that reads a bundle with `wft_compile.bundle.info` under the `submit` identity (decision 5).
- Owns: `wft_dispatchapi/{objects,resolver}.py`, `tests/objects/**`.
- Consumes / produces: artifact v3, store v4, `resolver_contract`.
  Produces the upload handler and the resolver for B.2 and C.3.
- Acceptance criteria:
  - [ ] `pytest packages/dispatchapi -v -k resolver_contract` passes against `StoreSourceResolver` over SeaweedFS, as `wft_jobcheck.resolver.resolver_contract` defines it.
  - [ ] `pytest packages/dispatchapi -v -k objects` passes, including `test_upload_twice_is_200_and_one_object`, `test_hash_mismatch_writes_nothing`, `test_object_too_large_per_kind`, `test_unreadable_bundle_refused` and `test_bad_kind`.
- Verifier: `tests/objects/`.

### B.2 Batch submit

Lane B, tier T1 (Postgres and SeaweedFS, marked `pair`). Needs: B.1, P.2. Blocks: B.3.

- Scope: the submit route over the sequence of D8 up to the answer line, with the template of D12, the class rule of decision 14 and the tenant and class check of D17, and one `job.accepted` per inserted row under D5's ids.
- Owns: `wft_dispatchapi/submit.py`, `tests/submit/**`.
- Consumes / produces: state v3, events v4, job v2, artifact v3.
  Produces accepted rows and events with links.
- Acceptance criteria:
  - [ ] `pytest packages/dispatchapi -v -k submit` passes, including `test_entry_outcomes_matrix` (new, same fingerprint, other fingerprint, duplicate index, unknown class, invalid parameters), `test_links_exist_before_rows`, `test_job_class_written_once`, `test_unknown_tenant_reads_no_body`, `test_one_template_manifest_per_batch`, `test_purged_object_stops_the_call` and `test_tenant_scoping`.
  - [ ] `pytest packages/dispatchapi -m slow -v -k submit_10000` passes: one call of 10,000 entries leaves 10,000 rows and 10,000 `job.accepted` events, and the same call resent answers `duplicate` for each and changes no count.
  - [ ] `pytest packages/dispatchapi -m slow -v -k submit_100000` passes, run with its output pasted before the PR that closes this issue merges (Q5): one call of 100,000 entries through the submit route leaves 100,000 rows and 100,000 `job.accepted` events, and the same call resent answers `duplicate` for each and changes no count.
    The test carries `slow` and `pair` and not `integration`, so neither `scripts/test.sh` nor `scripts/itest.sh` selects it and CI never runs it, a departure from the preamble's rule that a container test carries `integration`.
- Verifier: `tests/submit/`.

### B.3 Publish and reconciler

Lane B, tier T1 (Postgres with #7's `FakeBroker`, and Postgres with RabbitMQ from `wft_broker.testing.container.broker_container`, marked `pair`). Needs: B.2, E.3. Blocks: D.1.

- Scope: publishing after the answer lines with the receipt and `job.published` (D8), the reconciler of D7, both under the publish rules of decision 23, and the crash-window tests of the epic's level 2.
- Owns: `wft_dispatchapi/{publish,reconciler}.py`, `tests/publish/**`.
- Consumes / produces: broker v1 through `wft_broker.client`, A1 to A4 of section 2.
  Produces published rows.
- Acceptance criteria:
  - [ ] `pytest packages/dispatchapi -v -k publish` passes, including `test_crash_at_each_step_then_resend` (after the links, after the rows, after the answer, after the publish, after the receipt, each followed by a resend, a reconciler pass and a claim of every queued message by the test, with nothing lost and nothing doubled, where doubled counts rows and `job.accepted` and `job.published` events per job and never messages: at most two messages exist per job, and each extra one is acknowledged as a skip by the test's claim and never dead-lettered, while exit row 15 counts `job.admitted` and workflow executions too), `test_rejected_and_unroutable_leave_no_receipt`, `test_reconciler_rebuild_equals_stored_bytes` (the rebuild of a row whose event is stored equals it byte for byte and inserts nothing, and the rebuild of a row whose event was dropped is stored again), `test_one_published_event_when_reconciler_races_submit`, `test_reconciler_skips_a_row_with_a_receipt` and `test_reconciler_waits_out_the_grace`.
  - [ ] `test_publish_timeout_ends_the_call_and_the_pass` passes: under `FakeBroker.alarm(True)` a submit call and a reconciler pass each stop at their first `PublishTimeout`, every row keeps no receipt, and after `alarm(False)` the next pass publishes them all.
  - [ ] `test_reconnect_after_broker_unavailable` passes: after `FakeBroker.restart()` the publisher calls `connect` again, and the rows the lost connection left unpublished are published by the next pass.
  - [ ] `test_topology_error_stops_publishing` passes, over a connection that raises `TopologyError` because `FakeBroker` always has its exchange: the publisher and the reconciler stop with an error log and no retry, and the rows keep no receipt.
  - [ ] `pytest packages/dispatchapi -v -k publish_rabbitmq` passes with `wft_broker.client` as the user `dispatch` against `broker_container`, publishing to `intake.acme.standard`.
- Verifier: `tests/publish/`.

### C.1 Control routes

Lane C, tier T1 (Postgres with `FakeGateway`). Needs: P.1, E.3. Blocks: C.2.

- Scope: the single-job and batch control routes over P.1's function, the row writes and events of each cell in one transaction, the row-only events of decision 19 and the requested events, and the tenant check.
- Owns: `wft_dispatchapi/control.py`, `tests/control/test_routes.py`.
- Consumes / produces: state v3, events v4.
  Produces control requests for C.2.
- Acceptance criteria:
  - [ ] `pytest packages/dispatchapi -v -k "control and routes"` passes, including `test_cancel_and_pause_before_start_are_row_only`, `test_cancel_racing_the_claim_still_cancels` (a claim committed before the lock gives outcome `requested`, and a claim by `transition(accepted, admitted)` that waits on the lock loses with `Lost('cancelled')`, which the real claim of #8 answers as `Skipped('ended')`), `test_pause_and_cancel_racing_on_one_row`, `test_event_ids_follow_d5`, `test_batch_scope_answers_every_job` (each job in its own transaction, committed before its answer line, D19) and `test_another_tenants_job_is_not_found`.
  - [ ] `run_stage_contract` passes for the control stage over the events the tests wrote.
- Verifier: `tests/control/test_routes.py`.

### C.2 Gateway and control retrier

Lane C, tier T1 (Postgres with the Temporal dev server, marked `pair`, and `JobWorkflow` with `FakePool` and `FakeControl`). Needs: C.1. Blocks: C.3, D.1.

- Scope: `TemporalGateway` (D3), the retrier and the resume finisher of D6, the existence test of D4, and the agreement check of decision 12.
- Owns: `wft_dispatchapi/{gateway,retrier}.py`, `tests/control/test_{gateway,retrier,agreement}.py`.
- Consumes / produces: temporal v2, `wft_workflow.testing`.
  Produces delivered signals.
- Acceptance criteria:
  - [ ] `pytest packages/dispatchapi -v -k gateway_contract` passes against `TemporalGateway` on the dev server.
  - [ ] `pytest packages/dispatchapi -v -k retrier` passes, including `test_request_during_outage_delivered_after` (the dev server stopped, a cancel and a pause requested, the server started, both delivered and `control_delivered_at` set), `test_signal_twice_after_crash_is_harmless`, `test_resume_is_never_signalled` and `test_resume_pending_without_workflow_returns_to_accepted`.
  - [ ] `pytest packages/dispatchapi -v -k row_events_and_workflow_agree` passes with one case per control action, `cancel`, `terminate`, `pause` and `resume`, each on a running `JobWorkflow` and the resume sent by the test as admission would: once the action has reached its end, the row's status, the status that the job's latest lifecycle event projects through `contracts/status/status.yaml`, and the workflow (closed for `cancel` and `terminate`, its `status` query for `pause` and `resume`) agree.
- Verifier: `tests/control/test_gateway.py`, `test_retrier.py`, `test_agreement.py`.

### C.3 Resume with a patch

Lane C, tier T1 (Postgres with the dev server for the resume, and Postgres with SeaweedFS for the stored manifest, in separate tests marked `pair`, with the gateway faked where the store is real). Needs: C.2, E.3, B.1. Blocks: D.1.

- Scope: the patch flow of D18.
- Owns: `wft_dispatchapi/patch.py`, `tests/control/test_patch.py`.
- Consumes / produces: `wft_jobpatch.patch_check`, `set_pending_patch`, artifact v3.
  Produces `pending_patch_ref` for admission.
- Acceptance criteria:
  - [ ] `pytest packages/dispatchapi -v -k patch` passes, including `test_compatible_patch_sets_pending_ref`, `test_incompatible_patch_changes_nothing`, `test_invalid_patch_template`, `test_patch_not_started`, `test_second_patch_differs_is_pending`, `test_same_patch_again_is_noop`, `test_plain_resume_clears_the_pending_patch` and `test_patch_without_temporal_writes_nothing`.
- Verifier: `tests/control/test_patch.py`.

### R.1 Status and batch reads

Lane R, tier T1 (Postgres with `FakeGateway`). Needs: lane 0. Blocks: D.1.

- Scope: the job status, batch summary, job page and depth routes of 0.4, with D14 and Q2.
- Owns: `wft_dispatchapi/reads.py`, `tests/reads/**`.
- Consumes / produces: state v3, the reader login, the status query.
  Produces the read handlers.
- Acceptance criteria:
  - [ ] `pytest packages/dispatchapi -v -k reads` passes, including `test_view_from_requested_action`, `test_detail_unavailable_when_query_fails`, `test_results_in_task_order`, `test_dead_letter_field`, `test_page_is_ordered_and_bounded`, `test_depth_counts_accepted_by_class` and `test_reads_are_tenant_scoped`.
- Verifier: `tests/reads/`.

### D.1 Service

Lane D, tier T1 (Postgres and the dev server, marked `pair`, with the store faked and the broker `FakeBroker`). Needs: B.3, C.2, C.3, R.1. Blocks: D.2.

- Scope: `python -m wft_dispatchapi.service` with the two listeners of D1 and D2, the route table of section 3, held equal to `routes.yaml`, the loops of D6 and D7, the health and readiness of 0.4, the metrics of D15, the traces of 0.4, and a graceful stop that finishes the chunk in flight and then answers `ErrorLine`.
- Owns: `wft_dispatchapi/service/**`, `tests/service/**`.
- Consumes / produces: every lane's handlers.
  Produces the service.
- Acceptance criteria:
  - [ ] `pytest packages/dispatchapi -v -k service` passes, including `test_memory_with_four_calls_and_two_uploads` (the resident peak stays under 230 MiB), `test_every_route_on_its_listener_only`, `test_route_table_equals_routes_yaml`, `test_ready_only_when_dependencies_answer`, `test_metrics_names_and_no_id_labels`, `test_sigterm_mid_call_then_resend` and `test_sixteen_in_flight_then_too_many_calls`.
- Verifier: `tests/service/`.

### D.2 Image and deployment

Lane D, tier T2. Needs: D.1, the `admission` module of #8 and W.1 of `12-admission.md`. Blocks: the exit test.

- Scope: module `dispatch` as D16, its image on the `python` pin with the proton build stage, the login job for the four logins of D13 following `deploy/workflow/logins.py`, the store Secret of the `submit` identity, the broker Secret of the `dispatch` user from #7's password helper, two NodePort Services, readiness, the render tests, the `dispatch` line of `test_budget.py` (decision 24), and the smoke driver `python -m wft_dispatchapi.smoke` with the scenarios `upload`, `submit`, `cancel-before-start`, `run`, `pause-resume`, `cancel-running` and `restart`, each printing the lines that section 7 expects. The `run` and `pause-resume` scenarios upload the workload library bundle and run on the pool workers of #10 as decision 25 says, `run` with the level 0 `echo` params of `contracts/workload/vectors/level0/echo/input.json`, and `pause-resume` with one simulate task of the level 1 `cpu_burn` workload whose plan sets `checkpoint`, long enough to still run when the pause arrives. The `restart` scenario also prints `dlq:`, the count of messages in `intake.dlq` that `python -m wft_broker.dlq list` shows as the `operator` user, with its password from #7's helper.
- Owns: `deploy/dispatchapi/**`, `wft_dispatchapi/smoke/**`, the `TREES` entry, the `dispatch` entry of the reserve loop of `scripts/cluster/test_budget.py`.
- Consumes / produces: module v1, preset v1, the capacity revisit, the `rabbitmq` module of #7 and the pool workers of #10.
  Produces the deployed service.
- Acceptance criteria:
  - [ ] `pytest deploy/dispatchapi -v` passes: the render shows one replica with `Recreate`, NodePorts 30081 and 30080, requests and limits as D16, no credential in any manifest, the four logins and the broker Secret.
  - [ ] `pytest scripts/cluster -v -k budget` passes, with `test_ci_infra_node_figures` naming the `dispatch` module and still printing 502 MiB free on the `ci` infra node.
  - [ ] Under the cluster lock, `just up` and `scripts/tilt/run.sh ci` bring `dispatch` to ready, and `curl -s http://127.0.0.1:18081/readyz` and `curl -s http://127.0.0.1:18080/readyz` print `{"status":"ready"}`.
  - [ ] `python -m wft_dispatchapi.smoke --scenario submit --jobs 3` prints `accepted: 3`, `duplicate_on_resend: 3` and `unknown_tenant: 404`.
  - [ ] `pytest packages/migrate -v -k credentials` passes with the new tree.
- Verifier: `deploy/dispatchapi/test_render.py`, the smoke driver.

## 5. Order, stubs and spikes

- First, in parallel: 0.1 (issue), E.1, E.3 and S7, E.2 having landed by #429.
  Then 0.2 (issue).
  Lane 0 (0.1 and 0.2) merges alone, with an estimate of four days.
- After lane 0: P.1, P.2, R.1, and B.1 once E.1 has merged.
- After P.1 and E.3: C.1, then C.2.
  After P.2 and B.1: B.2, then B.3 once E.3 has merged.
  After C.2, E.3 and B.1: C.3.
  After B.3, C.2, C.3 and R.1: D.1.
  After D.1, the admission module and W.1 of `12-admission.md`: D.2.
- DAG: (0.1 ‖ E.1 ‖ E.3 ‖ S7), then 0.2, then (P.1 ‖ P.2 ‖ R.1 ‖ B.1), then (C.1 ‖ B.2), then (C.2 ‖ B.3), then C.3, then D.1, then D.2 once W.1 of `12-admission.md` has merged, then the exit test.
- Cluster work: D.2 and the exit test, each under the cluster lock, one at a time.

Stubs:

| Stub | What it fakes | How its conformance test runs against the real thing |
| --- | --- | --- |
| `wft_broker.testing.FakeBroker`, #7's own (decision 22) | RabbitMQ | #7's `broker_contract` holds it and the real client to one behaviour, and B.3's `publish_rabbitmq` runs the publisher against `broker_container` |
| `FakeGateway` | Temporal | C.2 runs `gateway_contract` against `TemporalGateway` on the dev server |
| Claims and `resume` signals sent by tests | admission (#8) | #8's built claim moves the row by the guarded move and sets a missing receipt (its lane W), it sends `resume` by decision 4, and the T2 rows of the exit test run with the real admission module |

Spikes: S7, as its block in section 4 states, runs beside lane 0 and gates nothing.

## 6. Test delta

Tests: each issue in section 4 names its tests and tier, T0 for the contract, the pure rules and the render tests, T1 for the submit path, the publisher, control, the reads and the service under `packages/dispatchapi/tests/`, and T2 for the smoke driver, all run by `just test`, `just itest` and `just ci`.
The one exception is B.2's `test_submit_100000`, which no recipe selects and which runs by hand before the PR that closes B.2 merges and in exit row 7 (Q5).
Commands: `python -m wft_dispatchapi.service`, `python -m wft_dispatchapi.smoke` with its scenarios at T2, `python -m wft_dispatchapi.contract` and `python -m wft_dispatchapi.gen`.
No helper script, verifier skill, feature file or verify recipe (decision 1).
What stays untested here is listed under "Not provable by this epic" in section 7, and the exit test proves the epic from a clean export.

## 7. Exit test

Run by a worker who did not write the lanes, on a clean `git archive` export of `main` taken outside any git repository, on the 20 GiB WSL2 host with Docker running for the whole run.
Containers other than the ones the run starts are never touched.
Each command runs from the export root, its real output is pasted in the PR that records the run, and it is summarised in Closure.
Every command takes `nix develop -c`, and every Python command takes `uv run --frozen`.
Rows 9 to 16 take the cluster lock for their whole sequence (`flock /tmp/wft-cluster.lock env WFT_CLUSTER_LOCK_HELD=1 bash -c '<commands>'`), and rows 17 and 18 run outside it.
`S` stands for `python -m wft_dispatchapi.smoke --dispatch http://127.0.0.1:18081 --control http://127.0.0.1:18080 --kube-context kind-wft`.
Rows 12 and 13 run on the real pool workers of #10, on `main` since it closed (assumption A8, decision 25).
W.1 of `12-admission.md` is on `main` before the run, because row 15 expects `dlq: 0` only from a claim that sets a missing receipt.
The export has no git checkout, so row 9 sets `WFT_BUILD_ID` for `scripts/tilt/run.sh ci`, which the workflow module requires outside one (`deploy/workflow/Tiltfile.module:15-17`), as the exit runs of #7 and #10 did.

| # | Command | Expected |
| --- | --- | --- |
| 0 | `grep -E '^(MemTotal\|MemAvailable):' /proc/meminfo` and `docker ps --format '{{.Names}}'`, repeated just before row 9 | `MemAvailable` at least 13,369,344 kB, the 12,032 MiB `memory_mib` of the `ci` preset plus 1,024 MiB for the smoke driver and the port-forwards, as the exit runs of #7 and #10 computed it, else the run stops and records the figure without touching other containers |
| 1 | `uv sync --frozen` then `uv lock --check` | exit 0 for both, lock unchanged |
| 2 | `for m in wft_dispatchapi.gen wft_workflow.gen wft_events.schemagen wft_jobstate.gen wft_storecheck.gen wft_compat.vectors; do python -m $m --check \|\| echo "DRIFT $m"; done` | no output |
| 3 | `for f in contracts/api/vectors/*/valid/*; do contracts/api/check.sh "$f"; done`, the same over every file under `contracts/api/vectors/*/invalid/` | exit 0 for each valid file, exit 1 with the `reason:` of its directory for each invalid one |
| 4 | `scripts/check-contracts-map.sh`, `scripts/export-diagrams.sh --check`, `just versions --check`, then the new diagram opened | exit 0 for each, and the diagram shows what 0.2 (issue) lists, checked by eye because an export gives every file one time |
| 5 | `pytest packages/dispatchapi packages/events packages/jobstate packages/storecheck packages/broker -q -rs` | all pass with no `SKIPPED` line and no `XFAIL` line |
| 6 | `pytest packages/dispatchapi -v -k "function_equals_control_yaml or entry_outcomes_matrix or links_exist_before_rows or crash_at_each_step_then_resend or reconciler_skips_a_row_with_a_receipt or publish_timeout_ends_the_call_and_the_pass or reconnect_after_broker_unavailable or cancel_racing_the_claim_still_cancels or request_during_outage_delivered_after or resume_is_never_signalled or incompatible_patch_changes_nothing or patch_not_started or detail_unavailable_when_query_fails or sigterm_mid_call_then_resend or resolver_contract or gateway_contract or publish_rabbitmq or row_events_and_workflow_agree"` | each named test is listed `PASSED` |
| 7 | `pytest packages/dispatchapi -m slow -v -k "submit_10000 or submit_100000"` | both `PASSED` |
| 8 | `pytest deploy/dispatchapi -v` | the render tests pass |
| 9 | `just up`, `scripts/tilt/run.sh ci`, `kubectl --context kind-wft -n wft-infra get pods`, `kubectl --context kind-wft -n wft-control get pods`, `kubectl --context kind-wft -n wft-workers get pods`, `curl -s http://127.0.0.1:18081/readyz`, `curl -s http://127.0.0.1:18080/readyz` | exit 0 with `rabbitmq` ready in `wft-infra`, `admission`, `workflow-worker` and `dispatch` ready in `wft-control`, and `compile-worker`, `simulate-worker` and `aggregate-worker` ready in `wft-workers`, and both probes print `{"status":"ready"}` |
| 10 | `S --scenario upload`, then `S --scenario submit --jobs 3` | `created: 1`, `already_present: 1`, then `accepted: 3`, `duplicate_on_resend: 3`, `unknown_tenant: 404` |
| 11 | `S --scenario cancel-before-start` | `row_only: 1`, `status: cancelled`, `workflow: none` |
| 12 | `S --scenario run --jobs 1` (the level 0 `echo` job of decision 25 on the workload library bundle) | `status: succeeded`, `results:` at least 1 |
| 13 | `S --scenario pause-resume` (one job whose simulate task runs the level 1 `cpu_burn` workload with `checkpoint` set in its plan, so the pause withdraws the running task, which goes back to `ready` with no checkpoint until #18, as the workers epic says (decision 25); this is not the checkpointed pause that decision 11 moves to the later pass of #17) | `seen: pausing,paused,resume-pending,running,succeeded` in that order |
| 14 | `S --scenario cancel-running` | `seen: cancelling,cancelled` |
| 15 | `S --scenario restart --jobs 2000` (the `dispatch` pod deleted mid-call, then the call resent) | `rows: 2000`, `accepted_events: 2000`, `lost: 0`, `doubled: 0`, where doubled counts rows, `job.accepted`, `job.published` and `job.admitted` events and workflow executions per job and never messages, `dlq: 0`, so every extra message ended acknowledged and counted by admission, and, as the backlog of rows with no receipt clears, `unpublished_rises: 0` and `unpublished: 0` within 600 s, as the note below the table says |
| 16 | `just down`, then `just nuke` twice | exit 0 for each, and `just nuke` leaves nothing and repeats cleanly |
| 17 | `just ci --full`, with `--full` because the export has no `origin/main` to pick the path from | exit 0 |
| 18 | the `ci` and `state-modules` workflows on `main` | one green run of each, the URLs recorded in Closure |

Row 15 proves that nothing is lost or doubled across a restart, not how fast the backlog drains.
The intake queue holds at most 1,000 messages and rejects a publish beyond that (`contracts/broker/topology.yaml`), so of the 2,000 jobs the rest reach the broker only as admission pulls messages and the reconciler publishes again, and the time that takes follows admission, which on `ci` pulls about 10.6 jobs/s until 1,000 jobs hold the global cap and then only as jobs finish, below 1 job/s ([#516](https://github.com/AndoNorth/weft/issues/516), [#519](https://github.com/AndoNorth/weft/issues/519)).
So the smoke driver counts the unpublished rows every second while it waits and prints the count every 10 s (`unpublished_at:`).
It fails if the count ever rises between two counts, which would mean a row lost its receipt or came back to `accepted` without one, and passes once the count reaches 0.
The 600 s bound only catches a backlog that never clears, and is more than twice the 265 s the passing run took.
Whether a pass should end at its first `REJECTED` publish, which would clear the backlog sooner, is [#518](https://github.com/AndoNorth/weft/issues/518) for #16.

Not provable by this epic: the `reconcile` command and 100,000 jobs per call on the cluster (#14, #16), the canonical end-to-end cases and the later pass with a patched resume from a checkpoint (#17, #18), a dead letter from the real admission and its replay, which #7's exit test proved with its suite (#8), the re-admission scan (#8), kills and outages beyond the service pod (#15), dashboards and alerts (#13), several replicas, and a tenant that is not self-asserted.

## 8. Parallelism and cost

- Maximum parallel workers: 4 in the first phase (0.1, E.1, E.3, S7), 1 for 0.2 beside the remaining lane E work, then 4 (P.1, P.2, R.1, B.1), then 2 (C.1 and B.2, then C.2 and B.3), then 1 for C.3, D.1, D.2 and the exit test.
- Host constraints: each T1 session holds at most two containers, so four lanes at once hold at most eight small containers.
  The cluster is single-owner, so D.2 and the exit test queue for the lock, behind the T2 work of #8.
- CI cost, estimates: no new workflow, and `just itest` gains about two minutes for the new T1 sessions and the slow 10,000-job test, while the 100,000-job test of B.2 runs only by hand (Q5).
- Estimates, not measurements, in worker-days: lane 0 (4: 0.1 2.5, 0.2 1.5), E.1 (0.5), E.2 (delivered), E.3 (2), S7 (1.5), P.1 (1.5), P.2 (1.5), B.1 (2), B.2 (4), B.3 (2.5), C.1 (3), C.2 (2.5), C.3 (2.5), R.1 (1.5), D.1 (2.5), D.2 (3), exit test (2).
  That is about 37 worker-days of effort and about 21 days elapsed along lane 0, P.1, C.1, C.2, C.3, D.1, D.2 and the exit test (4 + 1.5 + 3 + 2.5 + 2.5 + 2.5 + 3 + 2), with the admission module landing late, the cluster lock behind #8, and the Temporal client on a thread as the main sources of slip.

## 9. Closure

The exit test ran on 2026-11-19 against `60fc6aab932653ecf3cfa3d7dbf563919ce0c4aa`, the merge of D.2 and the last lane merge, from a `git archive origin/main` extracted into a scratch directory with no enclosing git repository.
Every row passes except row 15, which failed in the exit run and again in a rerun of row 15 alone.
Row 15 was then rerun alone with its window widened to 180 s and was red again, and passed in a rerun alone on the admission-independent expectation that section 7 now gives it (below), so the epic closes.
Row 17 failed once on the broker flake [#512](https://github.com/AndoNorth/weft/issues/512) and passed on its rerun.
The pasted output of every row is in the pull request that records this run.

Method of the run:

- The worker that ran it wrote none of the lanes.
- The host is the 20 GiB WSL2 VM with Docker running for the whole run.
  The one container not started by the run (`open-webui`) was left untouched.
- Rows 0 to 8 ran first, row 17 next, then rows 9 to 16 in one sequence under the cluster lock (`flock /tmp/wft-cluster.lock env WFT_CLUSTER_LOCK_HELD=1 bash <script>`) with row 0 repeated just before row 9, then row 17 again and the rerun of row 15 under the lock.
- A stopped `wft` cluster of preset `ci`, left by D.2, and its `wft-registry` were removed with `just nuke` under the lock before row 0's repeat, so row 9 started from no cluster.
- The export has no git checkout, so row 9 runs `scripts/tilt/run.sh ci` with `WFT_BUILD_ID=exit-a`, as section 7 says.
- Rows 5 to 8 and 17 ran with `WFT_SLOT=1`, the pool slot of the worktree the run was launched from ([#466](https://github.com/AndoNorth/weft/issues/466)), except row 8, which ran on slot 0 and waited for row 5's T1 lock files.
- Row 4's diagram was rendered to PNG with ImageMagick and read by eye.
- A memory sampler read `MemAvailable` every 5 s through rows 9 to 16.

Results:

| # | Result | Evidence |
| --- | --- | --- |
| 0 | pass | `MemAvailable` 18,368,856 kB at the start and 18,344,728 kB just before row 9, both above the gate of 13,369,344 kB, with only `open-webui` and the stale `wft-registry` running at the start |
| 1 | pass | `uv sync --frozen` exit 0, `uv lock --check` exit 0 with 75 packages resolved, the `uv.lock` SHA-256 `c59367e8...` unchanged |
| 2 | pass | no output from the six `--check` commands |
| 3 | pass | 41 valid files, each exit 0, and 84 invalid files, each exit 1 with the `reason:` of its directory, over the reasons `schema-violation` (54), `reason-scope` (10), `unknown-reason` (6), `control-cell-missing` (4), `transition-not-allowed` (4), `fingerprint-mismatch` (3), `event-not-of-stage` (2) and `too-large` (1) |
| 4 | pass | exit 0 for the three commands, `ok -- validation done` from `just versions --check`, and diagram 20 shows the two listeners on 18081 and 18080 in one process, the submit sequence of D8, the reconciler, the control table, the control retrier with its resume finisher, and admission alone sending `resume` with the pending patch |
| 5 | pass | 910 passed and 2 deselected (the `slow` tests) in 22 min 33 s, no `SKIPPED` and no `XFAIL` line |
| 6 | pass | 67 passed, each named test listed `PASSED`, `gateway_contract` as both `test_gateway_contract_holds_for_temporal` and the fake's own tests, and `function_equals_control_yaml` with its 37 broken-copy cases |
| 7 | pass | `test_submit_10000` and `test_submit_100000` `PASSED`, 9 min 18 s for both |
| 8 | pass | 17 passed, the 22 min 28 s of the run spent almost wholly waiting for row 5's T1 lock files |
| 9 | pass | `just up` in 59 s, `SUCCESS. All workloads are healthy.` after 234 s, `rabbitmq-0` Running in `wft-infra`, `admission`, `dispatch` and `workflow-worker-exit-a` Running in `wft-control` beside `placement`, the three pool workers Running in `wft-workers`, and `{"status":"ready"}` from both probes |
| 10 | pass | `created: 1`, `already_present: 1`, then `accepted: 3`, `duplicate_on_resend: 3`, `unknown_tenant: 404` |
| 11 | pass | `row_only: 1`, `status: cancelled`, `workflow: none` |
| 12 | pass | `status: succeeded`, `results: 2` in 66 s |
| 13 | pass | `seen: pausing,paused,resume-pending,running,succeeded` |
| 14 | pass | `seen: cancelling,cancelled` |
| 15 | pass on the third rerun | `answered_before_restart: 1000`, the stopping `TOO_MANY_CALLS` line, `resend: accepted 1000 duplicate 1000`, `rows: 2000`, `accepted_events: 2000`, `lost: 0`, `doubled: 0`, `dlq: 0`, then `unpublished: 365` against 0; the rerun of row 15 alone on a fresh cluster gave the same counts with `unpublished: 272` ([#516](https://github.com/AndoNorth/weft/issues/516)); a second rerun alone with the 180 s window gave the same counts with `unpublished: 106` ([#519](https://github.com/AndoNorth/weft/issues/519)); a third rerun alone on the expectation of section 7 gave the same counts with `unpublished_rises: 0` and `unpublished: 0` at 265 s, exit 0 |
| 16 | pass | exit 0 for `just down` and both `just nuke`, the second deleting no node, then `No kind clusters found.` with only `open-webui` running |
| 17 | pass on the second run | the first run's `just test` passed 3,215, and its `just itest` ended `1 failed, 771 passed` on `test_counters` of the broker, the flake [#512](https://github.com/AndoNorth/weft/issues/512); the second run exit 0 with `== ci path: full`, 3,215 passed in `just test` in 16 min 44 s, 772 passed in `just itest` in 40 min 25 s, and `all checks passed!` from `nix flake check` |
| 18 | pass | the `ci` run [44497098135](https://github.com/AndoNorth/weft/actions/runs/44497098135), success on `60fc6aa` on `main`, full path, 3,215 passed in `just test` and 772 in `just itest`, and the `state-modules` run [44497098049](https://github.com/AndoNorth/weft/actions/runs/44497098049), success on `60fc6aa`; no run on `main` holds this record yet, since it is the pull request that adds it |

Row 15's cause, from the rerun that sampled the intake queue and the job rows every 3 to 5 s:

- Nothing was lost or doubled: the depth of `intake.acme.standard` tracked the published rows at `accepted` (982 messages against 998 rows at 12:22:38Z), and the DLQ stayed empty.
- The queue sat at its `max-length` of 1,000 with `reject-publish` from 12:22:38Z to 12:23:55Z, so each publish beyond what admission had pulled was `REJECTED` and left to the reconciler, as decision 23 intends.
- Admission moved jobs off `accepted` at about 10.6 jobs/s and never reached the `ci` global cap of 1,000, so 2,000 jobs do not fill the cap and the queue at once, as D.2 assumed: the queue alone bounds the run.
- When the smoke's count began, 809 rows were unpublished, and publishing them needs admission to pull 809 messages first, at least 76 s at that rate, beyond the 60 s window.
- The reconciler refilled the queue in steps 20 to 30 s apart, because a `REJECTED` publish does not end a pass and most of each pass went on rejected publishes.
- The row's expectation is what #516 asks to change: a window derived from the queue bound, or a smaller job count, with the reconciler's pass on `REJECTED` a question for #16 ([#518](https://github.com/AndoNorth/weft/issues/518)).

Row 15 rerun with the 180 s window of section 7:

- It ran on 2026-11-19 from 12:34Z to 12:42Z on `main` at `8c0274f` with `UNPUBLISHED_S = 180.0`, from the task worktree, on a fresh cluster under the cluster lock: `just up`, `scripts/tilt/run.sh ci`, the smoke, then `just nuke` twice.
- Row 0's gate held with `MemAvailable` 18,291,356 kB, and row 9's checks passed with every pod Running and both probes `{"status":"ready"}`.
- The smoke printed `answered_before_restart: 1000`, the stopping `TOO_MANY_CALLS` line, `resend: accepted 1000 duplicate 1000`, `rows: 2000`, `accepted_events: 2000`, `lost: 0`, `doubled: 0`, `dlq: 0`, then `unpublished: 106` against 0, and exit 1.
- Its cleanup cancelled the batch with `applied 898`, `requested 1012` and `noop 90`, so about 1,100 jobs had left `accepted` in about 3.5 min, nearer 5 jobs/s than the 10.6 jobs/s the window assumed.
- At that rate the queue bound alone needs about 200 s, so the window still depends on how fast admission pulls ([#519](https://github.com/AndoNorth/weft/issues/519)).
- Both `just nuke` exited 0, the second deleting no node, then `No kind clusters found.` with only `open-webui` running.

Row 15 rerun on the admission-independent expectation of section 7, which counts the unpublished rows every second, fails if the count rises, and passes once it reaches 0 within 600 s:

- It ran on 2026-11-19 from 12:47Z to 12:56Z on the commit of this change, from the task worktree, on a fresh cluster under the cluster lock, as the rerun before it, with the intake queue depth and the job rows by status sampled every 3 to 5 s.
- Row 0's gate held with `MemAvailable` 18,325,096 kB, and row 9's checks passed with every pod Running and both probes `{"status":"ready"}`.
- The smoke printed `answered_before_restart: 1000`, the stopping `TOO_MANY_CALLS` line, `resend: accepted 1000 duplicate 1000`, then `unpublished_at:` 806 at 0 s, 572 at 20 s, 291 at 50 s, 62 at 70 s and 0 at 265 s, then `rows: 2000`, `accepted_events: 2000`, `lost: 0`, `doubled: 0`, `dlq: 0`, `unpublished_rises: 0`, `unpublished: 0`, and exit 0.
- Admission moved jobs off `accepted` at about 10.6 jobs/s, from 18 at 12:51:11Z to 1,000 at 12:52:44Z, when 1,000 admitted or running jobs held the `ci` global cap, and then only as jobs finished, about 0.75 jobs/s, to 1,143 at 12:55:53Z.
  The earlier reading of about 5 jobs/s from the cancel tallies was this cap averaged over the run.
- The queue sat between 782 and 997 messages until 12:52:43Z, so publishes beyond it were `REJECTED` and refilled by the reconciler in steps 20 to 30 s apart.
- The last 62 rows then stayed unpublished from 12:52:41Z to 12:55:56Z while the queue held 938 messages and fewer, so they would have fitted; the reconciler did not publish them for about 195 s, and the dispatch logs were not captured ([#520](https://github.com/AndoNorth/weft/issues/520)).
- Both `just nuke` exited 0, the second deleting no node, then `No kind clusters found.` with only `open-webui` running.

Memory figures:

- Host: `MemTotal` 20,482,272 kB, `MemAvailable` 18,344,728 kB at the gate before row 9, a low of 15,189,248 kB at 11:17:20Z at the end of row 15, 18,292,304 kB after row 16's last `just nuke`, 18,348,176 kB after the last `just nuke` of the row 15 rerun, in the rerun with the 180 s window a low of 14,711,588 kB at 12:41:58Z and 18,316,300 kB after its last `just nuke`, and in the passing rerun a low of 14,548,624 kB at 12:56:02Z and 18,304,004 kB after its last `just nuke`.

What was learned:

- The submit path, the publisher and the reconciler lost and doubled nothing across a pod deleted mid-call, twice, and the stopping pod ended the call with a line that says a resend is safe.
- On `ci`, the intake queue's `max-length` of 1,000 and admission, which stops at the global cap of 1,000 jobs and then follows the jobs that finish, bound how fast a large batch reaches the broker, so the restart case checks that the backlog only falls and clears, not how fast.
- A fresh `just up` leaves no current worker build, which the smoke driver sets itself (`smoke: made build exit-a current`), as the harness of #6 does.
- Row 8's T1 tests waited behind row 5 for the host-wide T1 lock files, so rows that hold T1 sessions run one after another whatever their slots.

What the architecture note now shows: `docs/architecture/20-dispatch-and-control.drawio` and its SVG are as 0.2 (issue) left them, and were re-checked by eye in this run for row 4.
The notes `04-intake-dedup-and-dead-letter.drawio`, `10-intake-and-admission.drawio` and `19-broker-intake-and-dead-letter.drawio` are unchanged.

Which assumptions of section 2 held: A1 to A6 and A10 as reconciled, A7 with the `admission` module Running in rows 9 to 15, A8 with rows 12 and 13 on the real pool workers, and A9 with `placement` Running beside the service and changing nothing it calls.
Questions Q2, Q3 and Q4 shipped at their defaults (a).

What moved to which epic, unchanged from section 7:

- the `reconcile` command and 100,000 jobs per call on the cluster (#14, #16)
- the replacement of the estimates of D6, D7, D10 and the module figures of D16, with S7's numbers as the first data point (#16)
- the canonical end-to-end cases and the later pass with a patched resume from a checkpoint (#17, #18)
- a dead letter from the real admission and its replay, and the re-admission scan (#8)
- kills and outages beyond the service pod (#15)
- the metric names of D15 in `metrics.yaml`, dashboards and alerts (#13)
- several replicas, and a tenant that is not self-asserted

Follow-ups found:

- [#516](https://github.com/AndoNorth/weft/issues/516) and [#519](https://github.com/AndoNorth/weft/issues/519): row 15's fixed window was beyond what a full intake queue drains on `ci`, settled by the admission-independent expectation of section 7.
- [#520](https://github.com/AndoNorth/weft/issues/520), moved to #16: the reconciler left 62 rows unpublished for about 195 s while the queue had room.
- [#518](https://github.com/AndoNorth/weft/issues/518), moved to #16: whether a reconciler pass should end at its first `REJECTED` publish, so the next pass refills the queue sooner.
- [#512](https://github.com/AndoNorth/weft/issues/512): the broker's `test_counters` failed again in row 17's first run, outside this epic.

Closure addenda, one dated line each, added after close when a `revisit` child issue is filed against this epic:

- 2026-11-19: [#542](https://github.com/AndoNorth/weft/issues/542) makes the dispatch smoke driver call `make_deployed_current` of `wft_workflow.harness.cluster` and import its job shape and API calls from `wft_acceptance.clients.api`, with its scenarios unchanged.
