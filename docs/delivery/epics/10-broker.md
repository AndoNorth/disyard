# Epic #7: Broker (RabbitMQ) - breakdown

Status: closed 2026-11-16
Epic: https://github.com/AndoNorth/weft/issues/7
Architecture note: docs/architecture/10-intake-and-admission.drawio, docs/architecture/04-intake-dedup-and-dead-letter.drawio, and a new broker intake and dead-letter diagram that 0.2 (issue) adds and numbers
Depends on (closed epics): Dev environment (#1), Fixed-resource cluster (#2), Job event envelope (#4), Durable state and artifact storage (#3), Workflow engine (#6)

Evidence marks: `[read]` means read on `main` at `6439283` on 2026-11-15 while preparing this spec, or at `9ee3066` on the same day for the review fixes, and nothing was executed beyond read-only commands and `scripts/cluster/budget.sh ci`.
`[looked up]` means read on 2026-11-15 from the PyPI JSON API for `python-qpid-proton` and `rabbitmq-amqp-python-client`, and from the Docker Hub tag list of the official `rabbitmq` image, and nothing was run against a broker.
`[unverified]` marks a broker behaviour that no spike or test in this repository has run, and that S4b or a named test settles before a lane relies on it.
The epic issue body was read on 2026-11-15 and is treated as stale where section 1 says so: where the body and landed code or a pinned contract disagree, the landed code and the contract win.
Method: [docs/delivery/method.md](../method.md).
Model: [docs/delivery/epics/08-workflow-engine.md](08-workflow-engine.md), whose structure this spec follows.
Tiers: T0 is host Python with no runtime, T1 is containers started through `wft_itest` (RabbitMQ from the pinned image, at most two containers across the host), and T2 is the cluster from `just up` on preset `ci`.
A test that needs a container carries the `integration` marker and runs in `just itest` and in every lane's `just ci`.
Command prefixes `nix develop -c` are omitted from every command of this spec and every command takes them, and every Python command also takes `uv run --frozen`.
T2 commands run under `flock /tmp/wft-cluster.lock env WFT_CLUSTER_LOCK_HELD=1 ...` as the cluster epic says.

## 0. Decisions

Section 0.1 lists what is already decided and carried, 0.2 the four questions the captain decided, 0.3 the defaults that the lanes build on, and 0.4 the proposals that lane 0 and the issues pin.
A change to a recorded decision changes only what its Applied in column names, and a change to a default only what its Changes column names, where an entry is an issue, a section of this spec or another decision or default.
"0.1 (issue)" and "0.2 (issue)" name the lane 0 issues of section 4, and a bare 0.1 to 0.4 names a part of this section.

### 0.1 Recorded decisions

| # | Decision | Source | Applied in |
| --- | --- | --- | --- |
| 1 | No verifier skill, no feature files and no verify recipes for this epic. Every issue names its tests and the exit test is a command checklist | the standing rule of every executed epic (`08-workflow-engine.md:32`), and the realignment of 2026-11-13 that retires `verify` and `verify-clean` (its decision D9), carried out by the capacity revisit #366, after which the method records the verifier convention as retired (`docs/delivery/method.md:149-152`) [read] | sections 4, 6 |
| 2 | Python models are the source of anything with a schema, and the exported JSON Schemas are checked in with a drift test | envelope epic decision 5 (`03-envelope.md:31`), carried by every later epic | 0.1 (issue), 0.4 |
| 3 | Runtime libraries read contract files through package data and never by repository path, and every package has a wheel test | state epic decision 11, carried (`08-workflow-engine.md:34`) | 0.1 (issue), 0.2 (issue), every package issue |
| 4 | AMQP 1.0 with the CloudEvents AMQP binding in structured mode and the JSON event format. The intake message is the `dev.wft.job.accepted.v1` event as one `data` section, with the content type `application/cloudevents+json; charset=utf-8`, and no context attribute is mapped to an AMQP property | envelope decision 1 (`03-envelope.md:27`), `contracts/events/README.md:42-45`, the stage table (`contracts/stages/stages.yaml:10,32`) [read] | 0.4, C.1 |
| 5 | The priority class set of the proof of concept is `standard` alone, the class that every event vector and test already uses, and tiers come later | realignment answer D5 of 2026-11-13, `packages/stages/src/wft_stages/facts.py:25` [read] | 0.4, T.1 |
| 6 | `INVALID_DAG` is dropped, because the plan is validated at the dispatch edge. `UNKNOWN_TENANT` is a rejection of the dispatch API (#24) only and never a dead-letter reason. The `job.dead.lettered` event keeps its two codes, `MALFORMED` and `UNSUPPORTED_VERSION`. A dead letter by delivery limit needs a signal that recovery can act on, so that each dead letter states clearly why it is there and how to recover it | realignment answer D6 of 2026-11-13, `contracts/events/schemas/job.dead.lettered/1.0.json:12-17`, `packages/events/src/wft_events/models.py:107` [read] | 0.4, Q1 |
| 7 | A dead-lettered job keeps its row at `accepted` so that a replay can admit it, admission emits `job.dead.lettered` only when the rejected message names a job, and a message with no derivable job id has no event and the dead-letter queue is its record. The body's "dead-letter notifier marks the job row" is superseded | `contracts/status/status.yaml:49-50`, `contracts/stages/stages.yaml:35-36`, `03-envelope.md:52` [read] | 0.4, Q1, the #8 rules row |
| 8 | Dead letters are kept for at least `DEAD_LETTER_RETENTION_DAYS` (14 days, a placeholder until real values are set), and a previous major stays readable for that retention plus the maximum job duration (21 days) | `packages/compat/src/wft_compat/window.py:5-16` [read] | D5, D7, 0.4, 0.1 (issue) |
| 9 | Replay republishes through `wft_compat.upgrade_for_replay`, and the S4 checks are re-run with the golden events of the envelope as payloads and its poison events as the malformed cases | envelope detail issue 9 and the hand-over to #7 (`03-envelope.md:87,127,209,337`), `packages/compat/src/wft_compat/reader.py:84` [read] | C.2, D.1 |
| 10 | No broker-side dedup. The unique key `(tenant, submission_key)` of the job row stops duplicates at submit, the publish receipt is `published_at`, and the reconciler of #24 republishes only rows without one | `packages/jobstate/src/wft_jobstate/migrations/0100_accepted_job.sql:16,27,31-32` [read] | 0.4, the #8 and #24 rules rows |
| 11 | The capacity revisit of #2, #366 (realignment answer D4), has landed: the `ci` infra node is 6,144 MiB, the quotas have no `limits.cpu` line, and `scripts/cluster/budget.sh` holds a RabbitMQ reserve row of 100m and 256 MiB of requests beside dispatch at 256 MiB and admission at 128 MiB, so no lane waits for it | realignment answer D4 of 2026-11-13, landed by `c8fa830` (https://github.com/AndoNorth/weft/pull/371): `deploy/presets/ci.yaml:18`, `scripts/cluster/budget.sh:35-39`, `02-cluster.md:268` [read] | D14, M.1, sections 2, 8 |
| 12 | The S4 checks are re-run on Python 3.13 before the client lane starts, because S4 ran on Python 3.14.6 while the repository pins 3.13 | `docs/delivery/spikes/broker-client.md:18`, `.python-version`, `pyproject.toml:4` [read] | S4b |
| 13 | Admission re-admits a job resumed before it started and a row stuck at `admitted`, which #8 owns. Neither case goes through the dead-letter queue | realignment answer D8 of 2026-11-13 | the #8 rules row |
| 14 | The epic body is refreshed as this breakdown starts (realignment answer D10), from section 1 of this spec | realignment answer D10 of 2026-11-13 | section 1 |
| 15 | The broker ships its own module under `deploy/rabbitmq/` against module contract v1, and its image source is this epic's choice. The body's "#2 provides the PVC" and "chart and image supply is an open item in #2" are superseded | `docs/delivery/epics/02-cluster.md:35-39`, `contracts/module/README.md:15-24` [read] | D13, M.1 |

### 0.2 Decided questions

Four questions, each with its options and the recommendation.
The captain accepted the recommended option (a) of each on 2026-11-15, so each is decided, and the other options stay only as the rejected alternatives.
Every decided option keeps every pinned contract as it is.

**Q1. What signal does a dead letter by delivery limit leave for recovery?**
A message reaches the delivery limit when a sixth delivery fails after five counted retries (D6), and perhaps also when a held delivery is lost to a broker restart or a dropped connection [unverified, S4b check 6].
The broker then dead-letters it with `x-last-death-reason=delivery_limit` and no application reason (S4 finding 2, `broker-client.md:50-52`), the job row stays `accepted` with `published_at` set, and no event names it, because the event enum has no such code.

- (a) Admission does not retry a `last` delivery: it dead-letters it itself with the application reason `ADMISSION_FAILED`, the error class as the detail token, and the job id in its own log, and writes no event, because `job.dead.lettered` 1.0 cannot carry the code.
  The reason table of the broker contract gains `ADMISSION_FAILED` with no event and the recovery `replay`, which changes no pinned contract because the broker contract is new.
  The broker's own `delivery_limit` death remains only for a last delivery lost to a crash, a restart or a dropped connection, and `dlq list` names the job of each from its body.
  The job status of #24 cannot tell such a job from one that waits in its intake queue.
- (b) As (a), plus an events contract-change PR (the next events version, `job.dead.lettered` 1.1 with the code `ADMISSION_FAILED`), which #8 files because it emits the event, so the event history of the job records the death and #24 can show it.
  This epic then changes only the Event column of one reason row.
  Consumers to list: #3, #8, #13, #24.
- (c) As (b), plus a dead-letter reporter in admission that reads each new dead letter and emits `job.dead.lettered` for every one that names a job, whatever the cause.
  It is the only option that covers a broker-only death, it is #8's component and estimate, and this epic only grants the `admission` user read on `intake.dlq`.

Recommendation: (a) for the proof of concept, which is also the default of the admission draft, because every dead letter then states why it is there and the dead-letter queue holds it until an operator replays or purges it, and (b) once #24's status must show a dead letter.
Decided: (a).

**Q2. Which client stack if S4b fails on Python 3.13?**
`python-qpid-proton` 0.40.0, which S4 used, publishes Linux wheels for no Python version, only an sdist and two macOS and Windows wheels, and the only later release, 0.41.1 of 2026-10-27, is an sdist alone [looked up].
So the dev shell and every image that uses the client build proton from source, and S4b decides whether that build and the five checks hold on Python 3.13.

- (a) Build proton 0.40.0 from its sdist in the dev shell and in a build stage of each image that needs it, and keep the S4 wrapper design.
- (b) Move to proton 0.41.1, if S4b passes on it where it fails on 0.40.0.
  The official `rabbitmq-amqp-python-client` 1.0.1 pins proton below 0.41.0 [looked up], so it could not be added beside it.
- (c) Fall back to AMQP 0-9-1 with a thin `pika` client behind the same interface, which S4 left untested and in which `basic.nack(requeue=true)` never counts toward the delivery limit (`broker-client.md:73-74`), so the counting retry needs a new design and a new spike.

Recommendation: (a), then (b) on an S4b fail that 0.41.1 passes.
Decided: (a), and (b) when S4b fails on 0.40.0 and passes on 0.41.1. When S4b fails on both, every code issue stops, because 0.2 (issue) waits for S4b, and the captain gets the S4b numbers to choose between (c) and another stack.

**Q3. Who may replay a dead letter?**

- (a) An operator, through `python -m wft_broker.dlq` on the host with the `operator` broker user, and nobody else.
- (b) The same, plus a replay route in the dispatch and control API of #24 for a submitter, scoped to the submitter's tenant.

Recommendation: (a), because a replay is a recovery after a fix that only the platform can make, and (b) adds a route, a broker user for the API and tenant scoping of the dead-letter queue.
Decided: (a).

**Q4. May a consumer return an intake message uncounted?**
The epic body bans `released` and `modified` with `delivery-failed=false` on intake queues because neither counts toward the delivery limit (S4 finding 7, `broker-client.md:59-60`), and says that admission defers by not granting credit.
Admission holds a pulled message that it cannot act on now for a cause outside the message: a dependency down, the job class of the message at its cap (known only after the pull, because queues are keyed by tenant and priority class), its own internal breaker open after one error class failed several messages in a row, or its own shutdown.
A counted `retry` then walks a healthy message toward the delivery limit, and holding it unsettled runs into the consumer timeout of the broker.

- (a) The client adds `release()`, which settles `modified` with `delivery-failed=false` and `undeliverable-here=false`, counts it in `wft_broker_settle_total{call="release"}`, and is for these four causes only: a dependency outage, a job class at its cap, admission's internal breaker open, and admission's drain on shutdown.
  Admission stops pulling from that queue until the cause clears, so a message is released at most once per outage, wait, breaker opening or shutdown.
  The internal breaker is the one cause that messages can trigger themselves, and a message that admission misreads as an outage could loop uncounted, which the release counter shows.
- (b) Keep the ban: admission settles such a message with a counted `retry`, so five outages or cap waits in a row dead-letter a healthy job.

Recommendation: (a), which the admission draft also needs (its D7, D8, D18 and Q4).
Decided: (a), with the place a released message takes in its queue measured by S4b check 13.

### 0.3 Defaults the lanes build on

These carry the remaining open questions of the epic and the choices found while reconciling it.
Numbers marked as estimates change only by the value PR of section 3, after #16 measures them, and #8 only reads the caps.

| # | Topic | Default | Reason, and the alternative | Changes |
| --- | --- | --- | --- | --- |
| D1 | Contracts (method contract 6, config half) | Two contracts, owner #7, version 1 each: `contracts/broker/` (topology names and numbers, settle and publish outcomes, the dead-letter reason table) and `contracts/tenancy/` (the tenant, priority class and cap configuration). Each has `VERSION`, `README.md`, `check.sh`, the pinned files, generated schemas and vectors under `vectors/<name>/{valid,invalid/<reason>}/`. Models live in `wft_broker.models` | #24 and #8 read the tenancy configuration without any broker semantics, and #8 may take over its caps later, so one owner per file is simpler than one file with two owners. The alternative is one `contracts/broker/` holding both | 0.1 (issue), 0.4 |
| D2 | Where the configuration lives (open question 1 of the epic, shared with #8 and #24) | One file `contracts/tenancy/tenancy.yaml` with one profile per preset (`ci`, `dev`, `soak`), read through package data by `wft_broker.tenancy.load(profile)`, as `contracts/job/limits.yaml` holds the limits profiles. The `ci` profile has the tenant `acme` (the tenant of every event vector) and the class `standard` (decision 5). A changed estimate is a value PR (section 3), and every other change, a tenant or a class included, is a contract-change PR that bumps the version | The body of #8 proposes one ConfigMap applied by restart, and `scripts/lint.sh:66-67` lints every YAML file under `deploy/` other than module manifests, presets, Tilt files and host ports as a Kubernetes manifest, so a configuration file there fails. #24 also checks tenants at T0, with no cluster. The alternative is a JSON ConfigMap under `deploy/rabbitmq/`, read only in the cluster | 0.4, T.1, the #8 and #24 rules rows |
| D3 | Caps (fields and first figures here, policy in #8) | The tenancy profile carries the fields the body of #8 names and the open cap its spec asks for: the ordered priority classes, the reserve interval (one admission in N for the lowest waiting class), an open cap (live and paused workflows), a global cap (live workflows), a cap per job class whose keys equal `classes` of the limits profile (`short`, `long`), held equal by a repository test because `wft_jobdoc` reads the limits file by repository path (`packages/jobdoc/src/wft_jobdoc/profile_files.py:10-20`), and a quota per tenant. Tenancy v1 pins the `ci` figures that #8 asks for (`12-admission.md:105`, its D1): `caps.open` 2,000, `caps.global` 1,000, every tenant quota 1,000, `caps.job_class` `short` 1,000 and `long` 100, and `reserve_interval` 5, every one an estimate. #8 only reads them, and a later figure comes from #16 by a value PR (section 3) | The epic gives this epic the format and #8 the policy (method contract 6, `docs/delivery/method.md:174,215`). Every admission lane is barred from `contracts/**` (`12-admission.md:293`), so the figures must be right when 0.1 (issue) pins them, and an open cap added later would be a shape change, tenancy v2, that #24 must also take. The rejected alternative is pinning this draft's figures without `caps.open` and leaving #8 to change them, which admission cannot do from its own lanes | 0.4, the #8 rules row |
| D4 | Topology | A direct exchange `wft.intake`, one quorum queue `intake.<tenant>.<class>` per tenant and priority class of the profile, bound with its own name as the binding key, a fanout exchange `wft.dlx` and one quorum queue `intake.dlq` bound to it. Every queue has an initial group size of 1 on the single node. In this spec an intake queue is an `intake.<tenant>.<class>` queue, and `intake.dlq` is not one | The tenant pattern `^[a-z0-9][a-z0-9-]{0,62}$` and the class pattern `^[a-z0-9][a-z0-9-]{0,31}$` hold no dot (`contracts/events/profile/profile.schema.json:68-70,84-87`), so a name maps to one pair and stays below the 255 bytes of a queue name. Publishing to the exchange keeps the outcome `released` for an unknown pair, while a publish to a missing queue address is a refused link | 0.4, T.1 |
| D5 | Queue settings by policy | Policy `wft-intake` on `^intake\.[a-z0-9][a-z0-9-]*\.[a-z0-9][a-z0-9-]*$`, applied to quorum queues: `delivery-limit` 5, `max-length` 1,000 (estimate), `overflow` `reject-publish`, `dead-letter-exchange` `wft.dlx`, `dead-letter-strategy` `at-least-once`. Once an intake queue holds more than `max-length` ready messages it rejects every publish until its ready messages fall to 80 % of `max-length` (800 at 1,000), as measured against the pinned image by `test_reconciler_against_a_full_queue` of `packages/dispatchapi/tests/publish/test_rabbitmq.py`. Policy `wft-dlq` on `^intake\.dlq$`: the delivery limit turned off [unverified, S4b check 9] and no `message-ttl`, so a dead letter leaves only by `dlq replay` or `dlq purge`, and decision 8's 14 days is the window inside which a replay of a previous major is sure to upgrade. Only the queue type is a declare argument | A policy can change without re-declaring a queue, and at-least-once dead-lettering needs `reject-publish` on the source queue. A TTL would delete the only record of a job whose row stays `accepted` with `published_at` set, which nothing else republishes or reports, so the alternative of a 14-day TTL loses jobs silently. 1,000 messages of about 0.5 KiB (S4's event was 487 bytes, `broker-client.md:40`) is about 0.5 MiB per queue, and the backlog of a large batch waits in Postgres as the body says | 0.4, T.1 |
| D6 | Delivery limit and delayed retry | Limit 5, so the sixth failed delivery dead-letters (S4 check 4). The body's delayed retry (type `all`, 1 s to 30 s) is added to `wft-intake` only if S4b check 7 shows that RabbitMQ 4.4.0 offers it on quorum queues and that it leaves the delivery count as S4 measured. Otherwise a redelivery is immediate. The broker contract does not depend on spaced retries: a counted retry stays counted whatever its spacing, and the limit of 5 stands. The risk is written down: admission sends a counted retry at once (`12-admission.md:111`, its D7), so without a delayed retry a transient error that admission does not class as an outage can spend all six deliveries within seconds and dead-letter a healthy job as `ADMISSION_FAILED`, which an operator replays. S4b check 7 measures whether the delayed retry exists, and the S4b result records which case holds | S4 never ran a delayed retry, and the spike's topology set only `x-delivery-limit` (`broker-client.md:22`). The rejected alternative is a rule that admission spaces its counted retries, which #8 declines (`12-admission.md:236`, its A4) because a counted retry is only for a failure of the message, its internal breaker catches a systematic failure, and a delivery is held for seconds and never minutes | T.1, S4b |
| D7 | Dead-letter queue (open question 5 of the epic) | One queue, `intake.dlq`, for every tenant, class and reason, with the reason in the annotations of each message, kept until replayed or purged (D5) and with no length bound | A broker dead-letters every message of a queue to one exchange whatever the reason, so one queue per reason would need routing on an annotation that S4 never tried. A length bound would make at-least-once dead-lettering hold messages in the intake queue once it is full. The alternative of one dead-letter queue per tenant adds queues that nothing reads apart | 0.4, T.1, D.1 |
| D8 | Settle calls | `ack()` settles `accepted`, `retry()` settles `modified` with `delivery-failed=true` and `undeliverable-here=false`, and `dead_letter(reason, detail)` settles `modified` with `delivery-failed=true`, `undeliverable-here=true` and the message annotations `x-opt-wft-reason` and `x-opt-wft-detail`. Under Q4 (a), `release()` settles `modified` with `delivery-failed=false` and `undeliverable-here=false`. The library sends no other outcome on an intake queue, so `released` and `rejected` cannot be sent | The body's three calls, `release()` per Q4, with `x-opt-wft-detail` added so that every application dead letter states why in a stable form (decision 6), and S4 findings 3 and 7 (`broker-client.md:53,59-60`) | 0.4, C.1, the #8 rules row |
| D9 | Reason of a dead letter | `x-opt-wft-reason` when present, `x-last-death-reason` otherwise, as S4 finding 2 says, and each code is a row of the reason table of 0.4 with its source, its event, whether it is expected on an intake queue and its recovery. `x-opt-wft-detail` starts with a stable token and `: `: the `reason` of the raised `wft_events` error for `MALFORMED` and `UNSUPPORTED_VERSION`, one of the admission tokens `wrong-type`, `unknown-tenant`, `unknown-class` and `queue-mismatch` for `MALFORMED`, or, for `ADMISSION_FAILED` (Q1), `no-row` for a job with no row or the error class otherwise | The `reason` of a typed event error is already the stable code of the event loader (`packages/events/src/wft_events/errors.py:3`, `contracts/events/README.md` Loader), so recovery can group dead letters by it without parsing free text | 0.4, D.1, the #8 rules row |
| D10 | Client library shape (detail issue 3 of the epic) | `wft_broker.client`, rebuilt from `broker-client.md` because the S4 code was never committed. A blocking API over one proton `Container` thread per connection: `connect(address, credentials) -> Connection`, `Connection.publish(routing_key: str, body: bytes, timeout_s: float = 5.0) -> Outcome` (5 s an estimate), `Connection.consumer(queue) -> Consumer`, `Consumer.pull(n, timeout_s)` and `Consumer.drain(n, timeout_s)` returning deliveries, and `Delivery.ack()`, `retry()`, `dead_letter(reason, detail)` and, under Q4 (a), `release()`. The receiver is created with credit 0 and gets credit only through `flow(n)` and `drain(n)`, and `receive()` and prefetch are never used. A refused link with `amqp:not-found` raises `TopologyError`, a publish with no outcome within `timeout_s` (a memory or disk alarm withholds publisher credit) raises `PublishTimeout` and leaves the connection usable, and a lost connection raises `BrokerUnavailable` on every pending call. There is no automatic reconnect: after a broker restart every call on the old `Connection` raises `BrokerUnavailable`, and the caller recovers by calling `connect` again. One `Connection` may be shared between threads for `publish`, and its publishes run one at a time. A `Consumer` and its deliveries belong to the one thread that opened the consumer | S4 findings 1, 6 and 7 (`broker-client.md:47-60`). An asynchronous caller wraps a call in `asyncio.to_thread`. The default timeout lets a caller that passes only the routing key and the body keep working. The official client 1.0.1 has no counting retry (the body's risk list) and pins proton below 0.41.0 [looked up]. Automatic reconnect would hide a lost delivery from the caller, whose loop already retries | 0.4, C.1 |
| D11 | Events in the library | The library never builds or parses an event: `publish` takes the bytes of `wft_events.to_bytes` or of a stored event, and a `Delivery` hands the consumer the body bytes, the delivery count, the queue and the received annotations, so the consumer decodes with `wft_compat.read_event`. The trace context travels in the `traceparent` attribute of the event only | The body's "CloudEvents structured encode and decode, trace propagation inside the event" duplicates `wft_events` and the required `traceparent` of the envelope | 0.4, C.1 |
| D12 | Broker users | Three users on vhost `/`, each with a password generated into the state directory: `dispatch` (write on `^wft\.intake$`), `admission` (read on the intake queue pattern of D5), and `operator` (read on `^intake\.dlq$` and on the intake queue pattern, write on `^wft\.intake$`). No user may configure, and the image's default `guest` user is not created. The broker module owns the passwords: a helper that M.1 ships returns a user's password and generates it into the state directory on first use, so whichever module loads first creates it. The users file of the definitions import, with salted password hashes, and the Erlang cookie go into the broker module's own Secret in `wft-infra`, and each consumer module creates its own Secret in its own namespace from the password the helper returns. The suite and the dead-letter tool read the same directory through `--credentials-dir`, and its file names are the helper's detail, stated once in the module README. Nothing is committed | A user per writer, as the app database has a role per writer (`contracts/state/README.md`, Writers), so a defect in dispatch cannot consume. `operator` reads the intake queues for the exit test of the suite only. The alternative of one shared user is simpler and makes the permission test of T.1 impossible. Generating on first use matters because the root `Tiltfile` loads modules in sorted path order (`Tiltfile:32-35,47-52`), so a consumer module under `deploy/` can load before `deploy/rabbitmq/` | 0.4, T.1, M.1, the #8 and #24 rules rows |
| D13 | Image | The official `rabbitmq` image 4.4.0, without the management plugin, pinned by digest as `images.rabbitmq` in `deploy/versions.yaml`, with `rabbitmq_prometheus` enabled through an `enabled_plugins` file. 4.4.0 is the newest release of the image on 2026-11-15 [looked up] and the version S4 ran | AMQP 1.0 is native in 4.x, the management UI costs memory on the infra node and no step reads it, and the queue counts that a test asserts come from the detailed metrics endpoint. The body's "image pin already targets 4.x" was false: `deploy/versions.yaml` has no RabbitMQ entry [read] | 0.2 (issue), T.1, M.1 |
| D14 | Runtime settings and footprint | Requests 100m and 256 MiB, limits 500m and 256 MiB (the reserve row of decision 11, CPU an estimate), a PersistentVolumeClaim of 1 GiB (estimate), the memory high watermark as `vm_memory_high_watermark.absolute` 154 MiB (0.6 of the limit, set absolute so it never depends on the memory the container detects), `disk_free_limit.absolute` 64 MB, `raft.wal_max_size_bytes` 32 MB (estimates), and the Erlang VM on one scheduler with busy waiting off (`+S 1:1 +sbwt none +sbwtdcpu none +sbwtdio none`). S4b check 10 measures the peak at that limit | S4 peaked at 154.6 MiB under a 1 GiB cap (`broker-client.md:20`), above 0.6 of 256 MiB, so the watermark could block publishers at the S4 load, and S8 set 256 MiB without a run (`docs/delivery/spikes/S8/state-footprint.md:161`). A peak above the watermark at the `ci` load moves the limit up in steps of 64 MiB, which changes the RabbitMQ reserve row of the capacity revisit and is raised with the S4b result | S4b, T.1, M.1 |
| D15 | Metrics and alerts (detail issue 7 of the epic) | Queue depth per queue comes from the detailed endpoint of `rabbitmq_prometheus` on port 15692, family `queue_coarse_metrics` [unverified, S4b check 11]. The client counts `wft_broker_publish_total{outcome}`, `wft_broker_settle_total{call}` and `wft_broker_dead_letter_total{reason}` in the registry of `wft_events.metrics`, which counts only the names pinned in `contracts/instrumentation/metrics.yaml` (`packages/events/src/wft_events/metrics.py:13,43-46`), so 0.1 (issue) adds the three to that file by a contract-change PR (instrumentation v1 to v2, consumers #8, #13 and #24). The counter labels are bounded: `outcome` is one of `accepted`, `rejected`, `unroutable`, `timeout`, `unavailable` and `topology-error`, `call` one of `ack`, `retry`, `dead_letter` and `release`, and `reason` a code of `reasons.yaml` whose source is `application`. The registry is in-process only (`packages/events/src/wft_events/metrics.py:1-4`), and #13 exports it from every process that loads it, dispatch and admission included. The three alert rules of 0.4 are stated in `contracts/broker/README.md`, and #13 installs them with the Prometheus it deploys | Naming #13 as the exporter is the smallest owner assignment, because the registry already exists for the codec counters that nothing exports either. The rejected alternative keeps the counters in process and moves the alert on them to #13, which still leaves the counters with no exporter. No Prometheus runs in the cluster before #13, a `PrometheusRule` manifest fails `kubeconform -strict` without its schema, and the instrumentation contract lets an epic extend `metrics.yaml` before #13 takes it over (`contracts/instrumentation/README.md:3-8`) | 0.4, 0.1 (issue), C.1, M.1, the #13 rules row |
| D16 | Replay (detail issue 5 of the epic) | `dlq replay` rebuilds each message from its bare sections (properties, application-properties, body) with no annotations (S4 findings 4 and 5), passes the body through `upgrade_for_replay`, publishes it to `wft.intake` with the routing key of its first-death queue, and settles the dead-letter copy `accepted` only after the publish answers `accepted`, so a crash duplicates and never loses. It refuses a reason whose recovery is `never` unless `--force`, and it never reads the job row, because admission decides from the row in any case (decision 7) and acknowledges a replayed message whose job has moved on | The body's "byte for byte" holds for a current major, which `upgrade_for_replay` returns unchanged, and decision 9 upgrades a previous major. The rejected alternative, a row check before replay, only saves an operator a useless replay and costs a database dependency, a Postgres container and a superuser DSN | 0.4, D.1 |
| D17 | Browsing the dead-letter queue | `dlq list`, `show`, `replay` and `purge` each make one pass: read the depth of `intake.dlq` once, grant credit up to that depth capped by `--limit` and 10,000 (estimate), hold every delivery unsettled until the pass ends, settle the selected ones, then settle the rest `released` together, which counts toward no limit since RabbitMQ 4.3 (the body's risk list). A pass never sees a message twice and never moves one it did not select. The library offers `released` only through a browser that it refuses to open on an intake queue | A quorum queue has no non-destructive read, and whether a released message keeps its place is [unverified, S4b check 9] | 0.4, C.1, D.1 |
| D18 | Regression suite (detail issue 8 of the epic) | `python -m wft_broker.suite` with the commands `check` (the five S4 checks and the negative control), `fill`, `drain`, `count` and `deadletter`, against any broker by `--address`, `--metrics` and `--credentials-dir`, and the same checks as pytest tests at T1. They run on every `just ci`, so a bump of the image, proton or Python fails CI before it merges | The body asks for the suite on each bump, and S4 found that delivery counting changed in 4.3. Queue counts are polled, because metrics lag by several seconds (S4 finding 8) | C.2, section 7 |
| D19 | Topology generation | `python -m wft_broker.topology --profile <name> [--users FILE] [--out DIR]` prints the topology definitions JSON from the tenancy profile and the broker contract, and with `--out` writes every file of `render` (`rabbitmq.conf`, `enabled_plugins`, the topology definitions and, with `--users`, the users definitions) into `DIR`, and the module and the T1 container both import it at boot from a directory that holds the topology file and the users file [unverified, S4b check 8]. Nothing generated is committed | One source, the tenancy profile, and no drift check needed. A definitions import adds and updates but never deletes, so removing a tenant is out of scope (section 1) | T.1, M.1 |
| D20 | T1 broker container | `wft_broker.testing.broker_container(t1, profile)` starts `images.rabbitmq` through `wft_itest` with the generated definitions, throwaway passwords in a temporary directory mounted read-only, ports 5672 and 15692 on the slot, and waits until every queue of the profile answers. #8 and #24 use it for their own T1 tests | `wft_itest` already starts any pinned image with read-only mounts and slot ports (`packages/itest/src/wft_itest/session.py:243-262`), and a 1 GiB cap per container (`:23`), above the 256 MiB of D14 | T.1, the #8 and #24 rules rows |
| D21 | CI for the new module | No new CI workflow and no change to `.github/workflows/`. The `state-modules` workflow's path filter lists no `deploy/rabbitmq/**` (`.github/workflows/state-modules.yml:7-16`), so M.1 runs `scripts/tilt/run.sh ci` itself and pastes the output, and the exit test records a green `state-modules` run on `main` | The workflow epic recorded the same gap as its default D25 (`08-workflow-engine.md:92`) | M.1, section 7 |
| D22 | Proton dependency | `python-qpid-proton==0.40.0`, the version S4 ran, built from its sdist (Q2). The C toolchain and headers that the build needs are checked in the dev shell by S4b, and an image that uses the client builds it in a build stage | No Linux wheel exists [looked up]. 0.41.1 is adopted only by a PR whose suite run passes on it | S4b, 0.2 (issue), C.1, the #8 and #24 rules rows |

### 0.4 Proposals the lanes pin

After lane 0 merges, `contracts/broker/README.md`, `contracts/tenancy/README.md` and their files are the source, and this section is only a pointer.

| Item | Proposal | Epic item |
| --- | --- | --- |
| Contract directories | `contracts/broker/` and `contracts/tenancy/` of D1. Loader `python -m wft_broker.contract --check FILE [--as NAME]` (exit 0 valid, 1 with `reason: <code>: <detail>` on stderr, 2 usage), behind each `check.sh`, and generator `python -m wft_broker.gen [--check]` | method contract 6, detail issues 2 and 4 |
| Package | `packages/broker/` (`wft_broker`): `models`, `contract`, `gen`, `tenancy`, `topology`, `client`, `dlq`, `suite` and `testing`, with `python-qpid-proton==0.40.0` as its one new dependency (D22) | detail issue 3 |
| Client | The API of D10, with `Outcome` in `ACCEPTED`, `REJECTED` and `UNROUTABLE`, `Delivery` carrying `body`, `delivery_count`, `queue`, `annotations` and `last`, and the settle calls of D8. `delivery_count` counts the failed deliveries before this one, 0 on the first delivery as S4 check 3 measured, and `last` is true when it equals the delivery limit, 5, on the sixth delivery, so a `retry` of it dead-letters | detail issue 3, D8 |
| Fake broker | `wft_broker.testing.FakeBroker`: an in-memory broker behind the same API, with the topology of a tenancy profile, credit, delivery counts, the delivery limit, `max-length` with `reject-publish`, `released` for an unknown routing key and dead-lettering with the annotations of D8. `wft_broker.testing.broker_contract(connect)` is the conformance test that runs against the fake in 0.2 (issue) and against the real client and container in C.1 | stub for #8 and #24 at T0 |
| Test container | `wft_broker.testing.broker_container(t1, profile)` of D20 | stub for nothing, a shared T1 fixture |
| Topology generator | `python -m wft_broker.topology` of D19 | detail issue 2 |
| Dead-letter tool | `python -m wft_broker.dlq {list,show,replay,purge}` with `--address`, `--credentials-dir`, selectors `--reason`, `--job`, `--limit`, and for `replay` `--force`, and for `purge` `--yes`. `list` prints one line per message with its position, reason, detail token for an application reason, `jobid`, `tenant` and `priorityclass` when the body parses, first-death queue and death time, then a count per reason of the messages it printed, so `--limit 1` prints one message line and one count line, and `show` prints one message in full. Exit 0 done, 1 a message was refused or a publish was not accepted, 2 usage, 3 the broker could not be reached | detail issue 5, D16, D17 |
| Regression suite | `python -m wft_broker.suite {check,fill,drain,count,deadletter}` of D18, where `check` refuses to start on an intake queue that holds a message or has a consumer (the consumer count of the detailed endpoint [unverified, S4b check 11]), `fill --count N` publishes golden events, `drain --count N` pulls, prints each delivery count and acks, `count` prints ready and unacked per queue from the metrics endpoint, and `deadletter` dead-letters one message for each reason that a consumer can cause (`MALFORMED` from a poison event, `UNSUPPORTED_VERSION` from `wrong-major.json`, `ADMISSION_FAILED` by a `dead_letter` of a `last` delivery, and `delivery_limit` by six retries) | detail issues 8 and 9 |

#### Broker contract detail

Files: `VERSION`, `README.md`, `check.sh`, `topology.yaml`, `reasons.yaml`, and the generated `topology` and `reasons` schemas.

`topology.yaml`:

| Key | v1 value | Rule |
| --- | --- | --- |
| `vhost` | `/` | one vhost |
| `intake_exchange` | `wft.intake`, direct, durable | D4 |
| `intake_queue` | template `intake.<tenant>.<class>`, quorum, initial group size 1, bound to `wft.intake` by its own name | one per tenant and priority class of the profile, D4 |
| `intake_policy` | name `wft-intake`, the pattern and keys of D5 | `delivery_limit` 5, `max_length` 1,000 marked `estimate: true` |
| `dead_letter_exchange` | `wft.dlx`, fanout, durable | D4 |
| `dead_letter_queue` | `intake.dlq`, quorum, initial group size 1, bound to `wft.dlx` | D7 |
| `dlq_policy` | name `wft-dlq`, delivery limit off, no message TTL | D5, and `dlq list` marks a dead letter older than `DEAD_LETTER_RETENTION_DAYS` as `stale` |
| `addresses` | publish to `/exchanges/wft.intake/<routing key>`, consume from `/queues/<queue>` | the AMQP 1.0 address format of RabbitMQ 4.x, used by S4 [unverified for the format name, S4b checks 1 to 5] |
| `message` | one `data` section, `content_type` `application/cloudevents+json; charset=utf-8`, `durable` true, no application-properties, no message annotations on publish | decision 4 |
| `annotations` | `x-opt-wft-reason` (a code of `reasons.yaml` whose source is `application`), `x-opt-wft-detail` (a token of D9, `: `, then text, at most 1,024 characters, as the `detail` of `job.dead.lettered`) | D8, D9 |
| `users` | `dispatch`, `admission`, `operator` with the permissions of D12 | D12 |
| `ports` | `amqp` 5672, `prometheus` 15692 | D13, D15 |

Publish outcomes:

| AMQP result | `Outcome` or error | Meaning | What the publisher does |
| --- | --- | --- | --- |
| `accepted` | `ACCEPTED` | the message is on its queue | records the receipt |
| `rejected` | `REJECTED` | the queue is full (`reject-publish`) | leaves the row unpublished for the reconciler |
| `released` | `UNROUTABLE` | no queue for the routing key, so the tenancy profile of the publisher and the broker topology differ | leaves the row unpublished and logs a defect |
| link refused with `amqp:not-found` | `TopologyError` | the exchange is missing | stops, a defect and never retried |
| no outcome within `timeout_s` | `PublishTimeout` | a memory or disk alarm, or a stalled broker | leaves the row unpublished for the reconciler |
| connection lost before the outcome | `BrokerUnavailable` | unknown | leaves the row unpublished, so a republish may duplicate, which admission's claim absorbs |

Settle calls, on an intake queue:

| Call | AMQP outcome | Counts toward the limit | Use |
| --- | --- | --- | --- |
| `ack()` | `accepted` | no, the message is gone | the workflow started, or the row says the message needs nothing |
| `retry()` | `modified`, `delivery-failed=true`, `undeliverable-here=false` | yes, and on a `last` delivery the broker dead-letters with `delivery_limit` | a failure of this message that may pass on a redelivery |
| `dead_letter(reason, detail)` | `modified`, `delivery-failed=true`, `undeliverable-here=true`, the two annotations | dead-letters at once with broker reason `rejected` | a message that can never be processed, or a `last` delivery under Q1 (a) |
| `release()`, under Q4 (a) | `modified`, `delivery-failed=false`, `undeliverable-here=false` | no | a dependency outage, a job class at its cap, the consumer's internal breaker open, or the consumer's drain on shutdown, and never a fault of the one message |

`reasons.yaml`, one row per code, keyed by code:

| Code | Source | Read from | On an intake queue | Event | Recovery | Operator action |
| --- | --- | --- | --- | --- | --- | --- |
| `MALFORMED` | application | `x-opt-wft-reason` | expected | `job.dead.lettered` with `dlq_reason: MALFORMED` when the body names a job | `never` | `show`, then `purge`, and end the named job through the control API |
| `UNSUPPORTED_VERSION` | application | `x-opt-wft-reason` | expected | `job.dead.lettered` with `dlq_reason: UNSUPPORTED_VERSION` | `after-fix` | deploy a reader of that major inside the window of decision 8, then `replay` |
| `ADMISSION_FAILED` | application | `x-opt-wft-reason` | expected | none under Q1 (a) | `replay` | find the cause from the detail token (`no-row`, or an error class) and the admission log of the job, fix it, then `replay` |
| `delivery_limit` | broker | `x-last-death-reason` | expected, when a last delivery is lost to a crash, a restart or a dropped connection | none | `replay` | check why the consumer lost it, then `replay` |
| `rejected` | broker | `x-last-death-reason` | not expected without `x-opt-wft-reason`, a consumer defect | none | `after-fix` | fix the consumer, then `replay` |
| `maxlen` | broker | `x-last-death-reason` | not expected, because intake queues reject publishes | none | `after-fix` | fix the topology, then `replay` |
| `expired` | broker | `x-last-death-reason` | not expected, because intake queues have no TTL | none | `after-fix` | fix the topology, then `replay` |

`dlq replay` refuses a code whose recovery is `never` unless `--force`.
Under Q1 (b) or (c) the `ADMISSION_FAILED` row gains its event, after the events bump that #8 files.

Alert rules stated in the README for #13, with the reason each one exists:

| Alert | Expression, on the detailed endpoint and the client counters | Why |
| --- | --- | --- |
| `WftDeadLetterQueueNotEmpty` | the ready messages of `intake.dlq` above 0 for 5 minutes | every dead letter needs a recovery decision |
| `WftIntakeQueueStalled` | an intake queue with ready messages and no consumer for 10 minutes | nothing admits the jobs of that queue, and a full queue alone is not alerted, because the backlog of a large batch waits in Postgres by design (D5) |
| `WftIntakePublishUnroutable` | `wft_broker_publish_total{outcome="unroutable"}` rising for 10 minutes | the topology differs from the tenancy profile of the publisher, a defect, while `rejected` is designed back-pressure and is not alerted |

Broker loader reasons, the first failing check winning in this order:

| Reason | Fails when |
| --- | --- |
| `schema-violation` | the file does not match its model |
| `bad-name` | a template, exchange, queue or policy name breaks D4 or D5 |
| `dlq-ttl-set` | the dead-letter policy carries a message TTL |
| `duplicate-code` | `reasons.yaml` lists a code twice |
| `event-code-mismatch` | an application row whose event is `job.dead.lettered` names a code outside the `dlq_reason` enum of that schema, read through package data, or a value of that enum has no such row |
| `unexpected-recovery` | a row not expected on an intake queue has the recovery `replay`, or an expected row has none |
| `bad-user` | a user may configure, or `dispatch` may read, or `admission` may write |

Vectors: the pinned files as valid files, and at least one invalid file under every reason above.

#### Tenancy contract detail

Files: `VERSION`, `README.md`, `check.sh`, `tenancy.yaml` and the generated `tenancy` schema.

`tenancy.yaml` holds `profiles`, keyed by the preset names, each with:

| Field | Type | Rule | `ci` value |
| --- | --- | --- | --- |
| `priority_classes` | list of strings | ordered from first served to last, each matching the `priorityclass` pattern of the event profile, at least one | `[standard]` (decision 5) |
| `reserve_interval` | integer | at least 2: one admission in this many goes to the lowest waiting class | 5 (estimate), the #8 body |
| `caps.open` | integer | at least `caps.global`, live and paused workflows across tenants | 2,000 (estimate) |
| `caps.global` | integer | at least 1, live workflows across tenants | 1,000 (estimate) |
| `caps.job_class` | map | keys equal `classes` of the same limits profile (`contracts/job/limits.yaml:22`), held by a repository test, values at least 1 and at most `caps.global` | `short: 1000`, `long: 100` (estimates) |
| `tenants` | map | keys match the `tenant` pattern of the event profile, at least one | `acme` |
| `tilt_profiles` | map | top level, not per profile: every profile of `deploy/tilt/profiles.yaml` to a tenancy profile | `minimal: ci`, `ci: ci`, `dev: dev`, `full: dev`, `soak: soak` |
| `tenants.<t>.quota` | integer | at least 1 and at most `caps.global`, live workflows of the tenant | 1,000 (estimate) |

`dev` and `soak` use the `ci` values, as the limits profiles do.
Every figure marked as an estimate above carries `estimate: true` in the file, as `max_length` does in `topology.yaml`, so a value PR (section 3) can tell which numbers it may change.
Loader reasons, in check order: `schema-violation`, `bad-tenant` and `bad-class` (a name breaks its event profile pattern), `cap-above-global` (a job class cap or a quota above `caps.global`, or `caps.global` above `caps.open`), and `unknown-profile` (a profile name other than `ci`, `dev` and `soak`).
Two repository tests, which read by repository path and so stay out of the loader, hold the keys of `caps.job_class` equal to the `classes` of the limits profile through `wft_jobdoc.profile_files.load_profile`, and the profile names equal to the presets of `deploy/presets/` and the keys of `tilt_profiles` equal to the profiles of `deploy/tilt/profiles.yaml`.
A module learns the Tilt profile from `WFT_TILT_PROFILE`, which M.1 exports from the root `Tiltfile` as it already exports `WFT_LIVE_UPDATE` (`Tiltfile:28`), and passes the mapped tenancy profile to its service as `WFT_TENANCY_PROFILE`, so the broker, admission and dispatch always read one profile.
Vectors: the pinned file as valid, and at least one invalid file under every reason.

### Rules agreed with epics that do not exist yet

These are stated so the owner epic confirms them in its own spec, and section 2 lists them again as assumptions for the morning review.

| Epic | Rule |
| --- | --- |
| #8 Admission | Consumes every intake queue of its tenancy profile with `Consumer.pull` or `drain` and one credit per pull, and never holds credit it cannot use. Acks only after the workflow start returned, for a row it claimed or a row already `admitted`, which it starts again, or after the row showed that the message needs nothing (a row past `admitted`, or `accepted` with an outstanding control action, which covers a duplicate publish, a cancel and a pause before start). A message whose job has no row is retried, and dead-letters with `ADMISSION_FAILED` and the detail token `no-row` on its last delivery. Decodes with `wft_compat.read_event` and dead-letters `MALFORMED` or `UNSUPPORTED_VERSION` with the detail token of D9, and emits `job.dead.lettered` with the same code when the body names a job. Dead-letters `MALFORMED` with `wrong-type` for a valid event that is not `job.accepted`, `unknown-tenant` or `unknown-class` for a pair outside its profile, and `queue-mismatch` when the `tenant` or `priorityclass` of the body differs from the queue. Uses `retry` only for a failure of the message itself, and dead-letters a `last` delivery with `ADMISSION_FAILED` instead of retrying it (Q1 (a)). Holds a delivery unsettled for seconds and never minutes, and settles a delivery in hand with `release()` for the four causes of Q4 (a) only: a dependency outage (Temporal or Postgres), a job class at its cap, its internal breaker open, and its drain on shutdown, then stops pulling from that queue until the cause clears, so an outage never dead-letters healthy jobs. Sends a counted retry at once, and accepts the risk that D6 states. Re-admits a job resumed before start and a row stuck at `admitted` from the row, never by a broker publish, so the `admission` user needs no write permission. Reads the caps of `contracts/tenancy/`, `caps.open` included, through `wft_broker.tenancy` and never changes them, because its lanes do not touch `contracts/**` (D3). Reaches the broker through the Service `rabbitmq` in `wft-infra` on port 5672, and makes its own Secret in its own namespace from the password that the broker's helper returns for the `admission` user (D12). Builds the client into its image with the proton build stage of D22. Its spec confirms the address, the Secret hand-off, the four release causes and the caps it reads, and does so in the #7 rules row of `12-admission.md` |
| #24 Dispatch and control | Checks the tenant and the priority class of each job against the tenancy profile and rejects an unknown tenant with its own `UNKNOWN_TENANT` code and an unknown class with its own `UNKNOWN_PRIORITY_CLASS` code at the edge (decision 6). Commits the row before it publishes, and publishes each accepted event with `Connection.publish("intake.<tenant>.<class>", body, timeout_s)` of D10 as the `dispatch` user, records the receipt only on `ACCEPTED`, and leaves the row unpublished on `REJECTED`, `UNROUTABLE`, `PublishTimeout` and `BrokerUnavailable`. Catches `PublishTimeout` in its submit handler and its reconciler, and ends a reconciler pass at the first one, because an alarm holds every publish. Recovers from `BrokerUnavailable` by calling `connect` again, since the client never reconnects by itself, and may share one `Connection` between its handler threads (D10). Reaches the broker through the Service `rabbitmq` in `wft-infra` on port 5672, and makes its own Secret in its own namespace from the password that the broker's helper returns for the `dispatch` user (D12). Reads pending depth per tenant and class from its own rows, not from the broker. A full intake queue is not shown to a submitter: the entry is answered as accepted and the reconciler publishes it later, which #24 confirms as its own API decision. Offers no replay route under Q3 (a). Builds the client into its image with the proton build stage of D22. Its spec confirms the publish signature with `timeout_s`, the handling of `PublishTimeout` and the reconnect, the address and the Secret hand-off. Pointer: the dispatch spec takes these in its decisions 8, 22 and 23 and its D16 (`13-dispatch-api.md`) |
| #13 Observability | Scrapes the detailed endpoint of D15, exports the registry of `wft_events.metrics` from every process that loads it, dispatch and admission included, takes over the three `wft_broker_*` counters that 0.1 (issue) adds to `metrics.yaml`, and installs the three alert rules of the broker README. Head-of-queue age is not available from quorum queues, so wait time comes from #8 |
| #15 Failure scenarios | Scales admission to zero before it drives the suite, which refuses a queue with a consumer, and drives broker restarts, poison messages and a full queue with `python -m wft_broker.suite` and `dlq`, and reads the restart result of C.2 as its baseline |
| #16 Characterization | Measures `max_length` of D5 and the caps of D3 and changes them by value PRs (section 3), measures the RabbitMQ footprint under the standard load against the 256 MiB of D14, and measures publish throughput of the reconciler against the client's one blocking publish at a time |
| #2 Cluster | Landed: the capacity revisit gives `wft-infra` the RabbitMQ reserve row of 100m and 256 MiB, which M.1's `module.yaml` matches, and drops the `limits.cpu` quota line, without which the 500m limit of D14 cannot be admitted (decision 11) |
| #17 Test ladder | The smoke and e2e passes publish through #24 and consume through #8 only, and use `dlq list` to show that a run left no dead letter |

## 1. Goal and non-goals

Goal: RabbitMQ is the intake buffer for submitted jobs and nothing else.
It holds each accepted event between the dispatch API and admission, keeps it durable across restarts, and gives an intake message that cannot be processed a dead-letter queue with a reason that says how to recover it.
The broker only orders and transports, it never owns job state, Temporal owns workflow lifecycle and the task backlog, and Postgres owns the job rows.

### Breakdown inputs updated for what earlier epics landed

The epic body was written on 2026-10-27, before the envelope, state and workflow epics closed, and these changes apply.

| Epic body item | Now | Source |
| --- | --- | --- |
| Application reasons `MALFORMED`, `INVALID_DAG`, `UNKNOWN_TENANT`, `UNSUPPORTED_VERSION` | `MALFORMED` and `UNSUPPORTED_VERSION` in the event, `ADMISSION_FAILED` in the broker contract only under Q1 (a), `UNKNOWN_TENANT` at the API edge, `INVALID_DAG` dropped | decision 6, Q1 |
| Detail issue 6, "dead-letter notifier marks the job row" | The row stays `accepted`, admission emits `job.dead.lettered`, and the delivery-limit case is Q1 | decision 7, Q1 |
| "The image pin already targets 4.x" | False: `deploy/versions.yaml` has no RabbitMQ entry, and 0.2 (issue) adds `images.rabbitmq` 4.4.0 | D13 [read] |
| Start from spike S4's `wft_amqp.py` and `s4.py` | Never committed: `git log --all` finds neither file, and only `docs/delivery/spikes/broker-client.md` exists, so C.1 and C.2 rebuild them from its findings | D10, D18 [read] |
| The wrapper passes on proton 0.40.0, and proton's last release was May 2025 | S4 ran on Python 3.14.6, and 0.40.0 has no Linux wheel, so it builds from source. Proton 0.41.1 was published on 2026-10-27 as an sdist only | decision 12, Q2, D22 [looked up] |
| "#2 provides the PVC", "chart and image supply for RabbitMQ is an open item in #2" | The broker ships its own module and chooses its own image | decision 15 |
| CloudEvents structured encode and decode, trace propagation inside the event | `wft_events` and `wft_compat` already do both, and the client handles bytes | D11 |
| DLQ retention | No TTL: a dead letter leaves only by replay or purge, and decision 8's 14 days is the replay window of a previous major | decision 8, D5 |
| Classes `high`, `normal`, `low` (open question 1, and diagram 10) | `standard` only for the proof of concept | decision 5 |
| "Generated from the same tenant and class config the admission controller reads" | No such configuration exists on `main` [read], so this epic pins its format as `contracts/tenancy/` | D1, D2, D3 |
| Delivery limit 5 with delayed retry (type `all`, 1 s to 30 s) | Limit 5 stands, and the delayed retry depends on S4b | D6 |
| Queue `max-length` (open question 2) | 1,000 per intake queue (estimate) with `reject-publish` | D5 |
| One dead-letter queue or one per reason (open question 5) | One | D7 |
| Replay republishes the bare message byte for byte | Byte for byte for a current major, upgraded for a previous one, and refused for a reason that never recovers | decision 9, D16 |
| The DLQ and depth alert rules | Stated here, installed by #13 | D15 |
| The #8 Interfaces row: "dead-letters with `INVALID_DAG` and `UNKNOWN_TENANT`" | Superseded by decision 6 | decision 6 |
| The settle contract: "`released` and `modified` with `delivery-failed=false` are banned", and the #8 row: "defers by not granting credit, never by `released`" | `released` stays banned, and `release()` sends an uncounted `modified` for the four causes of Q4 (a) only | Q4 |

### Delivered already, and what is left

| Epic body item | State on `main` | Where it is, or what is left |
| --- | --- | --- |
| Event codec, the 64 KiB cap, typed errors carrying `dlq_reason` | Delivered by #4 | `packages/events/src/wft_events/codec.py:19`, `errors.py:9-30` |
| Replay upgrade of a previous major | Delivered by #4 | `wft_compat.upgrade_for_replay` |
| Golden and poison events | Delivered by #4 | `contracts/events/vectors/golden/`, `contracts/events/vectors/poison/` |
| Job row, unique submit key, publish receipt | Delivered by #3 | `accepted_job` |
| T1 containers | Delivered by #1 and #4 | `packages/itest/` (`wft_itest`) |
| Module contract | Delivered by #2 | `contracts/module/` |
| Broker and tenancy contracts, the reason table | Left | 0.1 (issue) |
| Package, fake broker, image pin, refined diagrams | Left | 0.2 (issue) |
| Python 3.13 re-run of S4 | Left | S4b |
| Topology, broker configuration, T1 container | Left | T.1 |
| Client library, regression suite, failure cases | Left | C.1, C.2 |
| Dead-letter tool | Left | D.1 |
| RabbitMQ module | Left | M.1 |

Non-goals for this pass:

- everything the epic lists under "Does not own": job and task state, retries and the task backlog (#6), worker-facing broker traffic, dedup (#24 and #3), admission policy, credits, quotas, caps and priority order (#8), the intake message schema (#4) and the job row (#3), namespaces, quotas and the module contract (#2), and dashboards and alert rules other than the three this spec states (#13)
- a dead-letter reporter, under Q1 (a)
- removing a tenant or a priority class from a running broker, because a definitions import never deletes (D19) and the messages left in a removed queue would never be consumed, so a value PR never changes the tenant or class lists (section 3)
- more than one node, TLS, and authentication other than the three users of D12 on a cluster reached only through a port-forward bound to 127.0.0.1
- the management UI (D13) and head-of-queue age, which quorum queues do not report
- publish throughput of the reconciler and the standard load of 100,000 jobs, which are #24's optional S7 and #16's run

## 2. Interfaces

### Consumes (each pinned to a version)

| From | What | Pinned at | Stub used until it exists |
| --- | --- | --- | --- |
| Dev environment | Dev shell with `uv`, Python 3.13, `kubectl`, `kind`, Docker; recipes `just test`, `just itest`, `just ci`, `just up`, `just down`, `just nuke`; `wft_itest` | `flake.lock`, `uv.lock`, `.python-version` | none, exists [read] |
| Cluster | Preset `ci`, module contract, topology, namespace `wft-infra`, cluster lock | `deploy/presets/ci.yaml`, module v1, topology v1 | none, exists |
| Cluster, capacity revisit (#366) | The `ci` infra node at 6,144 MiB, no `limits.cpu` quota line, the RabbitMQ reserve row, `scripts/cluster/budget.sh` | preset `ci` at `c8fa830` | none, landed (decision 11) |
| Envelope | Event models and codec, typed errors with `dlq_reason`, `read_event`, `upgrade_for_replay`, golden and poison events, the instrumentation rules and the `wft_events.metrics` registry | events v3, compat v1, instrumentation v1, and the suite reads the golden events of whichever events version is on `main` | none, exists |
| Status and stages | A dead-lettered row stays `accepted`, admission emits `job.dead.lettered` | status v1, stages v1 | none, exists |
| State | `accepted_job` with `published_at`, read by the consumer and the publisher and never by this epic's code | state v2 | none, exists |
| Validator | The `classes` of the limits profile, read in a repository test through `wft_jobdoc.profile_files.load_profile`, which reads by repository path | job v2 | none, exists |
| Admission (#8) | The consumer of every intake queue | not built (E12) | none: this epic's own tests consume through the suite's `drain` and `deadletter` commands and C.1's tests (section 5) |
| Dispatch (#24) | The publisher | not built (E13) | none: this epic's own tests publish golden events through the suite's `fill` command (section 5) |
| Observability (#13) | Prometheus, the alert rules | not built (E15) | the exit test reads the detailed endpoint with `curl` |

No `revisit` child issue is open against an earlier epic that blocks a lane of this epic: the capacity revisit #366 of #2 is closed.
The names below were checked free on `main` [read: no `packages/broker/`, no `contracts/broker/`, no `contracts/tenancy/`, no `deploy/rabbitmq/`, no `docs/delivery/spikes/S4b/`].

### Produces (each pinned precisely)

| To | What | Where it lives (path) | Shape | Version |
| --- | --- | --- | --- | --- |
| #8, #24, #13, #15, #17 | Broker contract | `contracts/broker/` | `topology.yaml`, `reasons.yaml`, schemas, vectors, loader, the alert rules in its README, as 0.4 | broker v1 |
| #8, #24, #16 | Tenancy contract | `contracts/tenancy/` | `tenancy.yaml` with one profile per preset, schema, vectors, loader, as 0.4 | tenancy v1 |
| #8, #24 | Client library | `packages/broker/` (`wft_broker.client`) | the API of D10 and 0.4 | broker v1 |
| #8, #24 | Fake broker and its conformance test | `wft_broker.testing` | `FakeBroker`, `broker_contract`, `broker_container` | broker v1 |
| #8, #24 | Tenancy loader | `wft_broker.tenancy` | `load(profile)` returning the typed profile | tenancy v1 |
| #13 | Client counters and the detailed endpoint | `contracts/instrumentation/metrics.yaml`, port 15692 | D15 | instrumentation v2, broker v1 |
| operators, #15, #17 | Dead-letter tool | `python -m wft_broker.dlq` | 0.4 | broker v1 |
| #15, every bump | Regression suite | `python -m wft_broker.suite` | 0.4 | broker v1 |
| the cluster, #8, #24 | RabbitMQ module | `deploy/rabbitmq/` | module `rabbitmq` (module v1), image `rabbitmq` 4.4.0, the Service `rabbitmq` in `wft-infra` on ports 5672 and 15692, the password helper and the users of D12, the definitions of D19, the settings of D14 | module v1 |
| #2, #16 | Spike result | `docs/delivery/spikes/S4b/broker-client-py313.md` | pass or fail, numbers, decision | none |

### Assumptions about other epics

The breakdown specs of #8, #10 and #24 were drafted at the same time as this one.
Each assumption below is one the morning review checks against those specs, and each rules row of 0.4 is restated here.

| # | Epic | Assumption | If it does not hold |
| --- | --- | --- | --- |
| A1 | #2 | Held: the capacity revisit #366 landed as filed, with the `ci` infra node at 6,144 MiB, `wft-infra` and `wft-control` quotas without `limits.cpu`, a RabbitMQ reserve row of 100m and 256 MiB, and `scripts/cluster/test_budget.py` failing `just ci` when a real `module.yaml` does not fit | none |
| A2 | #8 | Admission consumes with the client of D10 under the #8 rules row: one credit per pull, ack after the start, of a claimed row or of a row already `admitted`, or after the row shows nothing to do, dead-letter codes and detail tokens of D9, `ADMISSION_FAILED` on a `last` delivery (Q1 (a)), `release()` for the four causes of Q4 (a), and re-admission from the row without a broker publish. The admission draft proposes the same in its D3, D7, D8, D9, D18, Q1 and Q4 | the reason table, the settle calls and the `admission` permissions of D12 change by a contract-change PR |
| A3 | #8 | Admission takes its caps, `caps.open` included, and its classes from `contracts/tenancy/` through `wft_broker.tenancy`, and only reads them. This spec pins the field and the `ci` figures that the admission spec asks for (D3), so admission keeps no caps of its own | two sources of the caps and the tenant list, which D2 exists to prevent |
| A4 | #8 | Admission emits `job.dead.lettered` itself (decision 7), and no other component emits it under Q1 (a) | Q1 (c) moves part of it to a reporter |
| A5 | #24 | Dispatch rejects an unknown tenant or class at the edge, publishes through `Connection.publish` of D10 as `dispatch`, handles `PublishTimeout` and reconnects after `BrokerUnavailable`, sets the receipt only on `ACCEPTED`, and reads pending depth from its rows. The dispatch draft pins `publish(routing_key, body)` without `timeout_s` and `PublishTimeout` (`13-dispatch-api.md:325`), which the default timeout of D10 keeps working, and its spec adds the handling | `UNROUTABLE` would become a normal outcome and `WftIntakePublishUnroutable` would fire on user error, and an unhandled `PublishTimeout` would fail a submit or stop the reconciler |
| A6 | #24 | Dispatch exposes no replay route (Q3 default) | Q3 (b) adds a broker user and tenant scoping of `dlq` |
| A7 | #8, #24 | Each builds proton 0.40.0 from its sdist in a build stage of its image (D22), on the pinned `python` image | a missing build stage fails their image builds, not this epic's |
| A8 | #13 | #13 exports the `wft_events.metrics` registry, installs the three alert rules of the broker README and takes over the `wft_broker_*` counters with `metrics.yaml` | the dead-letter queue has no alert until it does, so Q1 (a) loses its live signal, and the client counters stay in-process |
| A9 | #15, #16 | Both drive the suite and the dead-letter tool rather than their own broker clients, and #16 changes `max_length` and the caps by value PRs | duplicate broker code in the load and chaos tooling |
| A12 | #17 | The smoke and e2e passes publish through #24 and consume through #8 only, and use `dlq list` to show that a run left no dead letter | a test ladder with its own broker client |
| A13 | #24 | The dispatch draft refuses a class outside the tenancy profile with its own `UNKNOWN_PRIORITY_CLASS` code, publishes as A5 says, and starts from a fixture of the same loader shape until 0.1 (issue) merges | none for this epic |
| A10 | #10 | Workers never touch the broker, since pool traffic is Temporal's | none expected |
| A11 | all | The spike id S4b is not taken by a sibling spec of the same night, and the new diagram takes the next free number when 0.2 (issue) draws it, because `17` is already the admission cycle diagram and the dispatch spec also takes the next free number | renamed when the second PR merges |

## 3. Lanes

Package names are proposals.
Lane 0 owns every file it creates until the owning lane takes over the glob, except the contract modules of `wft_broker` and `wft_broker.testing`'s fake and conformance test, which stay lane 0's.

Departures from the method, stated so the review approves them knowingly:

- Spike S4b runs beside 0.1 (issue), because it writes only under `docs/delivery/spikes/`, and it gates 0.2 (issue), which adds proton to `uv.lock`.
- Lane 0 carries no `just/*.just` module and adds no recipe: tests run through `just test`, `just itest` and `just ci`, and the tools are `python -m` commands, as the toolchain and workflow epics did.
- 0.2 (issue) adds the `images.rabbitmq` entry of `deploy/versions.yaml`, where the module lane would usually own it, because the T1 tests of lanes T, C and D start that image before the module exists.
- Lane Needs run across lanes where a test over a stub would prove only the stub: C.1 runs against T.1's container, D.1 drives C.1's client, and M.1 deploys T.1's rendered configuration.
- A value PR changes a pinned number without a version bump, where the method asks for a contract-change PR with a version bump (`docs/delivery/method.md:101`).
  Its scope, stated here once: it changes only numbers marked `estimate: true` in a pinned file of `contracts/broker/` or `contracts/tenancy/` (`max_length` of `topology.yaml`, and the caps, quotas and `reserve_interval` of `tenancy.yaml`), touches only that file, runs its `check.sh`, and lists the consumers of that contract from section 2.
  It never adds or removes a field, a tenant or a class, and every other change is a contract-change PR.
  The reason is that #16 is expected to move every estimate, and a number changes nothing that a consumer parses.
- Lane 0 declares in `packages/broker/pyproject.toml` the dependencies of every later lane, where the method lands a dependency in the PR that needs it (`docs/delivery/method.md:84`), so that no later lane edits that file or `uv.lock`, the one hotspot, and the riskiest build, proton's, runs first in 0.2 (issue).

| Lane | Workers | Owns (exclusive) | Issues | Needs |
| --- | --- | --- | --- | --- |
| 0 Contracts and skeleton | 1 | `contracts/broker/**`, `contracts/tenancy/**`, `packages/broker/{pyproject.toml,README.md}`, `packages/broker/src/wft_broker/{__init__,models,contract,gen,tenancy}.py`, the package data under `packages/broker/src/wft_broker/contract/**`, `packages/broker/src/wft_broker/testing/{__init__,fake,conformance}.py`, `packages/broker/tests/{conftest,test_loader,test_drift,test_tenancy,test_reasons,test_fake_contract,test_wheel}.py`, the two `wft-broker` lines of the root `pyproject.toml`, the `uv.lock` entries, the three `wft_broker_*` entries and the version of `contracts/instrumentation/`, the `images.rabbitmq` entry of `deploy/versions.yaml`, the `broker` and `tenancy` rows of `docs/contracts.md`, `docs/architecture/15-contracts.drawio`, `10-intake-and-admission.drawio`, `04-intake-dedup-and-dead-letter.drawio` and the new broker diagram with their svgs | 0.1 (issue), 0.2 (issue) | nothing for 0.1 (issue), S4b for 0.2 (issue) (barrier, merged alone) |
| S Spike | 1 | `docs/delivery/spikes/S4b/**`, the S4b row of `docs/delivery/spikes/README.md` | S4b | nothing, beside 0.1 |
| T Topology and configuration | 1 | `packages/broker/src/wft_broker/topology.py`, `packages/broker/src/wft_broker/testing/container.py`, `packages/broker/tests/topology/**` | T.1 | lane 0 |
| C Client and suite | 1 | `packages/broker/src/wft_broker/{client,suite}.py`, `packages/broker/tests/{client,suite}/**` | C.1, C.2 | lane 0, T.1 for C.1, C.1 for C.2 |
| D Dead-letter tool | 1 | `packages/broker/src/wft_broker/dlq.py`, `packages/broker/tests/dlq/**` | D.1 | C.1 |
| M Module | 1 | `deploy/rabbitmq/**` (`module.yaml`, `Tiltfile.module`, the password helper of D12, the users and cookie script, the render test, the module README), the `WFT_TILT_PROFILE` line of the root `Tiltfile`, the `deploy/rabbitmq` entry of `TREES` in `packages/migrate/tests/test_credentials.py` | M.1 | T.1, S4b |

Shared-file rules:

- `uv.lock` is the one hotspot.
  0.1 (issue) commits it with the workspace entry of `wft-broker` and no new external dependency, and 0.2 (issue) adds `python-qpid-proton==0.40.0` after S4b.
  Lane 0 declares in `packages/broker/pyproject.toml` every dependency the lanes need (`pydantic`, `pyyaml`, and the workspace packages `wft-events`, `wft-compat`, `wft-jobdoc` and `wft-itest` in 0.1 (issue), and `python-qpid-proton` in 0.2 (issue)), so no later lane edits it.
  A lock conflict is never resolved by hand, and `uv lock` is rerun after rebase.
- If S4b shows that the dev shell lacks the C toolchain or a header that the proton build needs, 0.2 (issue) adds it to the dev shell in the same PR, which is this epic's only change under `nix/` or to `flake.nix`.
- The root `pyproject.toml` changes only in 0.1 (issue), by one `wft-broker` line in `[tool.uv.sources]` and one in the dev dependency group.
- `deploy/versions.yaml` changes only in 0.2 (issue), by the `rabbitmq` entry after `python`.
- `contracts/instrumentation/metrics.yaml` and its `VERSION` change only in 0.1 (issue), by a separate contract-change PR that adds the three counters of D15 with their bounded label values and lists #8, #13 and #24.
- The contract files of 0.1 (issue) stay lane 0's until 0.2 (issue) merges, and 0.2 (issue) applies to them every default that the S4b result changes.
- `packages/broker/src/wft_broker/__init__.py` and `packages/broker/tests/conftest.py` are lane 0's, and a lane imports by module path and keeps its own fixtures in a `conftest.py` inside its own test directory.
- A test that needs a container carries `integration`, and the restart tests of C.2 carry `isolated` because they restart their own container.
- `contracts/broker/README.md` and `contracts/tenancy/README.md` are lane 0's, and after lane 0 merges a pinned file changes only through a contract-change PR, or through a value PR within the scope that the departures above state.
- `docs/architecture/10-intake-and-admission.drawio`, `04-intake-dedup-and-dead-letter.drawio` and their svgs are refined by 0.2 (issue) before any other lane starts (`standard` in place of `high`, `normal` and `low`, the claim from `accepted` to `admitted` in place of "ack and drop: workflow id exists", and the reasons of the reason table), and the exit-test worker re-checks them at closure.
  Those refinements change only the broker's part of each diagram.
  The new broker diagram is self-contained: it shows only this epic's parts (the exchanges, the queues and their policies, the dead-letter path, the client calls and the dead-letter tool) and what each hands to its neighbours, and it names each neighbour by its role (dispatch, admission, operator, observability), not by a file, a lane or an epic number, so it reads without another document.
- `docs/delivery/spikes/README.md` gets one appended row.
- `packages/migrate/tests/test_credentials.py` changes only in M.1, by adding `deploy/rabbitmq` to `TREES` (`packages/migrate/tests/test_credentials.py:9-18`), so the credential scan covers the new module directory.
- No file under `deploy/rabbitmq/` other than `module.yaml` and Kubernetes manifests is YAML (`scripts/lint.sh:66-67`), so the broker configuration is rendered by `wft_broker.topology` at deploy and never committed.
- The root `Tiltfile` changes only in M.1, by one line that exports `WFT_TILT_PROFILE`.
- `justfile`, `just/*.just`, `.github/**`, `scripts/**`, `docs/dev-setup.md`, `deploy/host/ports.yaml`, every other package except the one `TREES` edit, and every other `deploy/` directory are not touched.
  Host access to RabbitMQ goes through `kubectl port-forward` bound to 127.0.0.1, and no NodePort row is added.
- Generated schemas are committed, regenerated by `python -m wft_broker.gen`, and never hand edited.
- No tracked file holds a credential: passwords and the Erlang cookie are generated into the state directory and delivered as Secrets (D12), as the `temporaldb` module does with its password (`deploy/temporaldb/Tiltfile.module:15-17`).

## 4. Issues

One block per issue, in the [lane issue template](../../../.github/ISSUE_TEMPLATE/lane-issue.md), replaced by `#NNN` links after filing.
Done means, for every issue: `just ci` is green on the branch, the output of the named tests is pasted in the PR, and nothing outside Owns and the shared files changed.
An issue that names a T2 command runs it under the cluster lock and pastes its output.

### Mapping from the epic's detail issues

| Epic detail issue | Delivered by |
| --- | --- |
| 1 Deploy RabbitMQ via Tilt | M.1 |
| 2 Intake topology as code | 0.1 (issue) (the format), T.1 |
| 3 Broker client library | S4b, C.1 |
| 4 Dead-letter reason taxonomy | 0.1 (issue) |
| 5 DLQ CLI | D.1 |
| 6 Dead-letter notifier | not built under Q1 (a), and under Q1 (b) or (c) the emitter or reporter is #8's, with only the reason row and the `admission` permission changing here |
| 7 Broker metrics and alerts | C.1 (the counters), M.1 (the detailed endpoint), 0.1 (issue) (the alert rules for #13) |
| 8 Delivery limit and delayed retry regression suite | S4b (delayed retry), C.2 |
| 9 Broker failure scenarios | C.2 (restart, connection drop, redelivery, poison, full queue at T1), section 7 (restart at T2), #15 for the chaos runs |

### 0.1 Broker and tenancy contracts

Lane 0, tier T0.
Needs: nothing.
Blocks: every other issue but S4b.

- Scope: the package skeleton in the workspace, the two contracts of 0.4 with their models, loader, generator, vectors and READMEs, the alert rules in the broker README, the three counters in `metrics.yaml` by their own contract-change PR, the two `docs/contracts.md` rows, and the contracts diagram.
- Owns: `contracts/broker/**`, `contracts/tenancy/**`, the `broker` and `tenancy` rows of `docs/contracts.md`, `docs/architecture/15-contracts.drawio` and its svg, `packages/broker/{pyproject.toml,README.md}`, `wft_broker/{__init__,models,contract,gen,tenancy}.py`, the package data under `wft_broker/contract/`, `tests/{conftest,test_loader,test_drift,test_tenancy,test_reasons}.py`, the two root `pyproject.toml` lines, the workspace entry of `uv.lock`, the three `wft_broker_*` entries and the version of `contracts/instrumentation/`.
- Consumes / produces: events v3 (the `job.dead.lettered` enum), compat v1 (`DEAD_LETTER_RETENTION_DAYS`), job v2 (the `classes` of the limits profiles, read by repository path in a test only), the first two read through package data.
  Produces broker v1 and tenancy v1.
- Acceptance criteria:
  - [ ] `contracts/broker/check.sh` and `contracts/tenancy/check.sh` exit 0 for every `vectors/*/valid/*` file and the pinned `topology.yaml`, `reasons.yaml` and `tenancy.yaml`, and 1 with the `reason:` of its directory for every `invalid` one, shown by `pytest packages/broker -v -k loader`.
  - [ ] `test_reasons_match_event_enum` passes on the pinned table and fails on the vectors under `invalid/event-code-mismatch/`, including one that adds `INVALID_DAG`.
  - [ ] `test_dlq_policy_has_no_ttl` passes, and the loader refuses the vector under `invalid/dlq-ttl-set/`.
  - [ ] `test_tenancy_job_classes_match_limits` passes, and fails when a limits profile gains a class that `caps.job_class` lacks.
  - [ ] `python -m wft_broker.gen --check` exits 0, and exits 1 after a model field is renamed.
  - [ ] `test_tenancy_profiles_match_presets` passes, and fails on a profile named after no file of `deploy/presets/`.
  - [ ] `contracts/instrumentation/check.sh` or its loader test passes with the three new counters, and `python -m wft_events.schemagen --check` exits 0.
  - [ ] `scripts/check-contracts-map.sh` and `scripts/export-diagrams.sh --check` exit 0.
- Verifier: `packages/broker/tests/test_loader.py`, `test_drift.py`, `test_tenancy.py`, `test_reasons.py`.

### 0.2 Package skeleton, fake broker, image pin and diagrams

Lane 0, tier T0.
Needs: 0.1 (issue), S4b.
Blocks: every code issue.

- Scope: the proton dependency and the lock, every default of the contract files that the S4b result changes, the `images.rabbitmq` pin of D13, `FakeBroker` and `broker_contract`, the wheel test, the refined diagrams 10 and 04, and the new broker diagram, self-contained as the shared-file rules of section 3 say.
- Owns: the rest of lane 0's globs, and the contract files of 0.1 (issue) until it merges.
- Consumes / produces: the S4b result, the broker and tenancy contracts.
  Produces the stub of section 5 and the image pin.
- Acceptance criteria:
  - [ ] `uv sync --frozen` builds proton 0.40.0 in a clean dev shell, and `uv lock --check` exits 0.
  - [ ] `just versions --check` exits 0 with the new entry, whose digest equals the one S4b ran.
  - [ ] `pytest packages/broker -v -k fake_contract` passes against `FakeBroker`, and `broker_contract` fails against fakes that over-deliver on one credit, settle a retry with `delivery-failed=false`, dead-letter without the reason annotation, or accept a publish to an unknown routing key.
  - [ ] `pytest packages/broker -v -k wheel` builds the wheel and loads both contracts from package data outside the repository.
  - [ ] `scripts/export-diagrams.sh --check` exits 0 after the refinements.
- Verifier: `test_fake_contract.py`, `test_wheel.py`.

### S4b The S4 checks on Python 3.13, and what S4 never ran

Lane S, spike, timebox 1.5 days (estimate), tier T1, throwaway code in `docs/delivery/spikes/S4b/code/`, result in `docs/delivery/spikes/S4b/broker-client-py313.md`.
Needs: nothing.
Blocks: 0.2 (issue), M.1.

- Question: does the S4 wrapper design hold on Python 3.13 with proton 0.40.0 built from its sdist, against `rabbitmq` 4.4.0 pinned by digest, and what do the settings of D5 to D19 that S4 never ran do?
- Pass line, binary: checks 1 to 5 of S4 pass with every assertion in three consecutive runs on the Python 3.13 of the dev shell, and proton 0.40.0 builds from its sdist both under `uv` in the dev shell and in a build stage on the pinned `python` image (`deploy/versions.yaml:34-37`).
- Measurements, each changing a default and not the pass: 6 whether a delivery held across a dropped connection and across a broker restart counts toward the limit (the body's risk list), 7 whether 4.4.0 offers a delayed retry on quorum queues and how it changes the counts (D6), 8 a definitions import from a directory holding a topology file and a users file with salted hashes, with the policies of D5 applied (D19, D12), 9 the `wft-dlq` policy with the delivery limit off, and whether a browse that settles `released` keeps the order and counts nothing (D5, D17), 10 the memory peak and any watermark alarm at a 256 MiB limit with 1,000 messages in each of four intake queues and 1,000 in `intake.dlq` (D14), 11 the series names of the detailed endpoint for ready and unacked messages per queue (D15), 12 the same five checks on proton 0.41.1, for Q2 (b), and 13 where a message settled `modified` with `delivery-failed=false` goes back in its quorum queue, head or tail, and that its delivery count stays (Q4).
- Acceptance criteria:
  - [ ] The result file states pass or fail against the pass line, the numbers of every measurement, and the default each one confirms or changes.
  - [ ] `pytest docs/delivery/spikes/S4b/code -q` or the spike's own runner prints the assertion counts of the three runs, pasted in the PR.
- Verifier: the spike's runner, and the result file.

### T.1 Topology, broker configuration and the T1 container

Lane T, tier T1.
Needs: lane 0.
Blocks: C.1, M.1.

- Scope: `wft_broker.topology.render(profile, users)`, which returns the files of a broker configuration (`rabbitmq.conf` with the settings of D14, `enabled_plugins`, the topology definitions and the users definitions), its command line of D19, and `broker_container` of D20.
- Owns: lane T's globs.
- Consumes / produces: broker v1, tenancy v1, `images.rabbitmq`.
  Produces the configuration that M.1 deploys and the container that lanes C and D and the #8 and #24 epics use.
- Acceptance criteria:
  - [ ] `pytest packages/broker -v -k topology_matches_tenancy` passes: the rendered definitions hold exactly one queue per tenant and class of the profile, the two exchanges, the bindings and the two policies, and adding a tenant to a vector profile adds one queue.
  - [ ] `pytest packages/broker -v -m integration -k policies_applied` passes: in a container, every intake queue carries `wft-intake` with the limit, length, overflow, exchange and strategy of D5, and `intake.dlq` carries `wft-dlq`, read from the broker and not from the file.
  - [ ] `pytest packages/broker -v -m integration -k users_permissions_enforced` passes: `dispatch` cannot attach a receiver, `admission` cannot publish, `operator` cannot declare, and `rabbitmqctl list_users`, run through `Container.exec`, lists exactly `dispatch`, `admission` and `operator`, because a refused `guest` login over the Docker bridge proves nothing (`loopback_users`).
  - [ ] `pytest packages/broker -v -k rendered_files_hold_no_secret` passes: no rendered file other than the users definitions holds a password or hash.
- Verifier: `packages/broker/tests/topology/`.

### C.1 Client library

Lane C, tier T1.
Needs: lane 0, T.1.
Blocks: C.2, D.1.

- Scope: `wft_broker.client` of D10 and 0.4, with the outcome and error mapping, the settle calls, the `last` flag, the `released` browser of D17 refused on intake queues, and the counters of D15.
- Owns: `wft_broker/client.py`, `tests/client/**`.
- Consumes / produces: broker v1, `broker_container`.
  Produces the client that #8, #24, D.1 and C.2 use.
- Acceptance criteria:
  - [ ] `pytest packages/broker -v -m integration -k fake_and_real_pass_contract` runs `broker_contract` against the real client and container, and passes.
  - [ ] `pytest packages/broker -v -m integration -k "publish_outcomes or one_message_per_credit or drain_empty_returns_zero or retry_counts or dead_letter_on_sixth_delivery or reason_in_same_settle or missing_exchange_is_topology_error or browser_refused_on_intake or last_delivery_flagged or release_uncounted or publish_times_out_under_alarm or publish_from_threads"` lists each test `PASSED`, where `publish_from_threads` publishes from eight threads through one `Connection` and gets one outcome per publish.
  - [ ] `test_banned_outcomes_not_offered` passes: no public call of the client settles `released` or `rejected` on an intake queue, and `release()` is the only call that sends `modified` with `delivery-failed=false`.
  - [ ] `publish_times_out_under_alarm` forces an alarm with `rabbitmqctl set_vm_memory_high_watermark absolute 1MB` through `Container.exec` and expects `PublishTimeout`, and the next publish on the same `Connection` after the alarm clears is `ACCEPTED`.
  - [ ] `test_counters` passes: a publish of each outcome, each settle call and a retry of a last delivery move the three counters of D15 with the right labels.
- Verifier: `packages/broker/tests/client/`.

### C.2 Regression suite and failure cases

Lane C, tier T1, with two `isolated` tests.
Needs: C.1.
Blocks: the exit test.

- Scope: `wft_broker.suite` of D18, the golden events through publish, dead-letter and replay, the poison events dead-lettered as `MALFORMED`, a full queue, and restarts and connection drops with a held delivery.
- Owns: `wft_broker/suite.py`, `tests/suite/**`.
- Consumes / produces: the golden and poison events of the envelope, the client.
  Produces the suite that every bump runs and that #15 drives.
- Acceptance criteria:
  - [ ] `pytest packages/broker -v -m integration -k "golden_vectors_round_trip or replay_byte_identical or replay_strips_death_annotations or poison_vectors_dead_letter_malformed or full_queue_rejects or negative_control_fails"` lists each test `PASSED`, and the negative control shows that the byte check can fail.
  - [ ] `pytest packages/broker -v -m isolated -k "restart_keeps_messages or held_delivery_across_restart"` passes: messages survive a container restart, every call on the `Connection` opened before it raises `BrokerUnavailable` and a new `connect` publishes and pulls again, and the delivery count of a delivery held across it matches what S4b check 6 recorded.
  - [ ] `pytest packages/broker -v -m integration -k held_delivery_across_connection_drop` passes: a connection closed by the broker (`rabbitmqctl close_connection`) while a delivery is held raises `BrokerUnavailable`, and the delivery count on the next pull matches what S4b check 6 recorded.
  - [ ] `python -m wft_broker.suite check` against a `broker_container` exits 0 and prints the assertion count.
- Verifier: `packages/broker/tests/suite/`.

### D.1 Dead-letter tool

Lane D, tier T1.
Needs: C.1.
Blocks: the exit test.

- Scope: `python -m wft_broker.dlq` of 0.4, D16 and D17.
- Owns: `wft_broker/dlq.py`, `tests/dlq/**`.
- Consumes / produces: the client, the reason table, `wft_compat.upgrade_for_replay`.
  Produces the operator tool.
- Acceptance criteria:
  - [ ] `pytest packages/broker -v -m integration -k "dlq_list_reasons or dlq_list_moves_nothing or dlq_replay_upgrades_previous_major or dlq_replay_refuses_never or dlq_replay_publish_before_settle or dlq_purge_needs_yes or dlq_show_and_selectors"` lists each test `PASSED`, the last running `show`, `--job` and `--limit`.
- Verifier: `packages/broker/tests/dlq/`.

### M.1 RabbitMQ module

Lane M, tier T2.
Needs: T.1, S4b.
Blocks: the exit test.

- Scope: module `rabbitmq` in `wft-infra`: `module.yaml` with the figures of D14 and readiness as `deploy/temporaldb/module.yaml:15-17` has it, a StatefulSet with one replica and its PersistentVolumeClaim, the Service `rabbitmq` for ports 5672 and 15692, a ConfigMap from `wft_broker.topology.render`, the password helper of D12 and a Secret with the users definitions and the Erlang cookie from the state directory, the pod's readiness probe, and a render test.
- Owns: lane M's globs.
- Consumes / produces: module v1, the rendered configuration, the reserve row of the capacity revisit.
  Produces the module, its Service and the password helper that the consumer modules call.
- Acceptance criteria:
  - [ ] `contracts/module/check.sh deploy/rabbitmq/module.yaml` exits 0, and the capacity revisit's budget check passes with the real module in place of the reserve row.
  - [ ] `pytest deploy/rabbitmq -v` passes: the rendered StatefulSet carries the requests, limits, Erlang flags and volumes of D14, the ConfigMap holds no secret, a changed tenancy profile rolls the pod, and the password helper creates a password in an empty state directory and returns the same one on the next call.
  - [ ] Under the cluster lock, `just up` then `scripts/tilt/run.sh ci` exits 0 with `rabbitmq` ready, and `kubectl --context kind-wft -n wft-infra exec rabbitmq-0 -- rabbitmqctl list_queues name type policy messages` lists `intake.acme.standard` and `intake.dlq` with their policies, and `rabbitmqctl list_users` lists exactly `dispatch`, `admission` and `operator`.
  - [ ] Through a port-forward bound to 127.0.0.1, `python -m wft_broker.suite check` exits 0 against the module.
  - [ ] `pytest packages/migrate -v -k credentials` passes with `deploy/rabbitmq` in `TREES`.
- Verifier: `deploy/rabbitmq/test_render.py`, the pasted T2 output.

## 5. Order, stubs and spikes

- First, in parallel: 0.1 (issue) and S4b.
  Then 0.2 (issue).
  Lane 0 (0.1 (issue) and 0.2 (issue)) merges alone, with an estimate of three and a half days.
- After lane 0: T.1.
- After T.1: C.1 and M.1.
- After C.1: C.2 and D.1.
- DAG: (0.1 (issue) ‖ S4b), then 0.2 (issue), then T.1, then (C.1 ‖ M.1), then (C.2 ‖ D.1), then the exit test.
- Cluster work: M.1 and the T2 rows of the exit test, each under the cluster lock, one at a time.

Stubs:

| Stub | What it fakes | How its conformance test runs against the real thing |
| --- | --- | --- |
| `FakeBroker` | the client and a broker with the topology of a profile, for #8 and #24 at T0 | `broker_contract` runs against the fake in 0.2 (issue) and against the real client and container in C.1 |

The suite's `fill`, `drain` and `deadletter` commands stand in for the publisher and the consumer in this epic's own tests only.
No other epic builds code against them, and #15 and #16 drive them as tools, so they are not stubs and carry no conformance test of their own, and `broker_contract` already covers the client calls they share with the publisher and the consumer.

Spikes: S4b, as its block in section 4 states, runs beside 0.1 (issue) and gates 0.2 (issue) and M.1.

## 6. Test delta

Tests: each issue in section 4 names its tests and tier, T0 and T1 under `packages/broker/tests/`, and T0 under `deploy/rabbitmq/`, all run by `just test`, `just itest` and `just ci`, with the S4b runner under `docs/delivery/spikes/S4b/code/`.
Commands: `python -m wft_broker.contract`, `wft_broker.gen`, `wft_broker.topology`, `wft_broker.suite` and `wft_broker.dlq`, the two `check.sh` loaders, and the password helper of D12.
No helper script under `scripts/`, verifier skill, feature file or verify recipe (decision 1).
What stays untested here is listed under "Not provable by this epic" in section 7, and the exit test proves the epic from a clean export.

## 7. Exit test

Run by a worker who did not write the lanes, on a clean `git archive` export of `main` taken outside any git repository, on the 20 GiB WSL2 host with Docker running for the whole run.
Containers other than the ones the run starts are never touched.
Each command runs from the export root, its real output is pasted in the PR that records the run, and it is summarised in Closure.
Every command takes `nix develop -c`, and every Python command takes `uv run --frozen`.
Rows 9 to 15 take the cluster lock for their whole sequence (`flock /tmp/wft-cluster.lock env WFT_CLUSTER_LOCK_HELD=1 bash -c '<commands>'`), and row 16 runs outside it.
`S` stands for `python -m wft_broker.suite --address 127.0.0.1:<amqp> --metrics http://127.0.0.1:<prom>/metrics/detailed --credentials-dir <state directory>/rabbitmq`, and `Q` for `python -m wft_broker.dlq --address 127.0.0.1:<amqp> --credentials-dir <state directory>/rabbitmq`, over two `kubectl --context kind-wft -n wft-infra port-forward --address 127.0.0.1 pod/rabbitmq-0` forwards to 5672 and 15692 that row 10 opens and row 15 closes.

| # | Command | Expected |
| --- | --- | --- |
| 0 | `grep -E '^(MemTotal\|MemAvailable):' /proc/meminfo` and `docker ps --format '{{.Names}}'`, repeated just before row 9 | `MemAvailable` at least the `memory_mib` of the `ci` preset plus 1,024 MiB, which is 12,928 MiB (13,238,272 kB) after the capacity revisit, else the run stops and records the figure without touching other containers |
| 1 | `uv sync --frozen` then `uv lock --check` | exit 0 for both, lock unchanged, proton 0.40.0 built |
| 2 | `for m in wft_broker.gen wft_events.schemagen wft_jobdoc.schemagen wft_compat.vectors; do python -m $m --check \|\| echo "DRIFT $m"; done` | no output |
| 3 | `for c in broker tenancy; do for f in contracts/$c/vectors/*/valid/*; do contracts/$c/check.sh "$f"; done; done`, the same over every file under `contracts/{broker,tenancy}/vectors/*/invalid/` | exit 0 for each valid file, exit 1 with the `reason:` of its directory for each invalid one |
| 4 | `scripts/check-contracts-map.sh`, `scripts/export-diagrams.sh --check`, `just versions --check`, then diagrams 10 and 04 and the new broker diagram opened | exit 0 for each, and the diagrams show `standard`, the claim in place of "workflow id exists", and the reasons of the reason table, checked by eye because an export gives every file one time and the diagram check compares times |
| 5 | `pytest packages/broker -q -rs`, then `pytest packages/broker -m isolated -q -rs` | both pass with no `SKIPPED` line, and the second runs the two restart tests of C.2 |
| 6 | `pytest packages/broker -v -k "reasons_match_event_enum or dlq_policy_has_no_ttl or tenancy_job_classes_match_limits or tenancy_profiles_match_presets or fake_and_real_pass_contract or topology_matches_tenancy or policies_applied or users_permissions_enforced or rendered_files_hold_no_secret or publish_outcomes or one_message_per_credit or drain_empty_returns_zero or retry_counts or dead_letter_on_sixth_delivery or reason_in_same_settle or missing_exchange_is_topology_error or browser_refused_on_intake or last_delivery_flagged or release_uncounted or publish_times_out_under_alarm or banned_outcomes_not_offered or counters or golden_vectors_round_trip or replay_byte_identical or replay_strips_death_annotations or poison_vectors_dead_letter_malformed or full_queue_rejects or negative_control_fails or held_delivery_across_connection_drop or dlq_list_reasons or dlq_list_moves_nothing or dlq_replay_upgrades_previous_major or dlq_replay_refuses_never or dlq_replay_publish_before_settle or dlq_purge_needs_yes or dlq_show_and_selectors or publish_from_threads"` | each named test is listed `PASSED` |
| 7 | `pytest deploy/rabbitmq -v` | the render tests pass |
| 8 | `python -m wft_broker.topology --profile ci` | the definitions of one intake queue, `intake.acme.standard`, the two exchanges, the bindings and the two policies, and no user |
| 9 | `just up`, `scripts/tilt/run.sh ci`, `kubectl --context kind-wft -n wft-infra get pods`, `kubectl --context kind-wft -n wft-infra exec rabbitmq-0 -- rabbitmqctl list_queues name type policy messages` | exit 0 with every module of the `ci` profile ready, `rabbitmq` among them, and `intake.acme.standard` and `intake.dlq` listed as quorum queues with their policies and 0 messages |
| 10 | scale the admission Deployment of `wft-control` to zero replicas when admission's module is on `main`, then the two port-forwards, then `S check` | no admission pod remains, or the run records that admission's module is not on `main`, and `S check` exits 0, every assertion passed, and the intake queue and `intake.dlq` empty again at the end, which also shows that no consumer was attached |
| 11 | `S fill --count 20`, `kubectl --context kind-wft -n wft-infra delete pod rabbitmq-0`, wait for ready, re-open the forwards, `S count`, then `S drain --count 20` | `count` shows 20 ready on `intake.acme.standard` after the restart, and `drain` prints 20 deliveries |
| 12 | `S deadletter`, then `Q list` | four lines: `MALFORMED` with its detail token and `-` for the job, because a poison body does not parse, `UNSUPPORTED_VERSION` and `ADMISSION_FAILED` with their detail tokens and job ids, and `delivery_limit` with `-` for the detail and its job id, then the count per reason |
| 13 | `Q list --limit 1`, `Q show --job <the job id of the ADMISSION_FAILED line>`, `Q replay --reason delivery_limit`, `Q replay --reason MALFORMED`, `Q purge --reason MALFORMED --yes`, `S drain --count 1`, `Q list` | the first prints one message line and one count line, `show` prints that message with its annotations, the first replay replays one message, the second refuses with exit 1, the purge removes one, the drain receives the replayed event byte-identical to its golden file, and the last list shows the `UNSUPPORTED_VERSION` and `ADMISSION_FAILED` lines only |
| 14 | `curl -s 'http://127.0.0.1:<prom>/metrics/detailed?family=queue_coarse_metrics'` filtered for the two queues | ready-message series for `intake.acme.standard` at 0 and `intake.dlq` at 2 |
| 15 | `Q purge --reason UNSUPPORTED_VERSION --yes`, `Q purge --reason ADMISSION_FAILED --yes`, close the forwards, `just doctor`, then `just down`, then `just nuke` twice | exit 0 for each, and `just nuke` leaves nothing and repeats cleanly |
| 16 | `just ci` | exit 0, on the full path |
| 17 | the `ci` and `state-modules` workflows on `main` | one green run of each, the URLs recorded in Closure |

Not provable by this epic: publishing from the real submit sequence and reconciler (#24), consuming with the real admission policy, the start call and the `job.dead.lettered` event (#8), Prometheus scraping and the alert rules (#13), broker chaos under load (#15), the standard load and the footprint at 100,000 jobs (#16), and a broker on more than one node.

## 8. Parallelism and cost

- Maximum parallel workers: 2 in the first phase (0.1 (issue) and S4b), 1 for 0.2 (issue), 1 for T.1, then 2 (C.1 and M.1), then 2 (C.2 and D.1), then 1 for the exit test.
- Host constraints: T1 work holds at most two containers across the host, and lanes T, C and D hold one RabbitMQ container each.
  `wft_itest` takes one of two host-wide lock files per container (`packages/itest/src/wft_itest/session.py:21,48-50`), so C.2 and D.1 in parallel queue for a lock rather than start a third container.
  The cluster is single-owner, so M.1 and the T2 rows of the exit test queue for the lock, and `ci` needs 11,904 MiB of the 20 GiB host after the revisit.
- Budget on the `ci` infra node: the module takes exactly the RabbitMQ reserve row of the capacity revisit (100m and 256 MiB requested), so the worst case stays at the 566 MiB free that `scripts/cluster/budget.sh ci` prints today, and a larger figure from S4b check 10 is raised before M.1 starts.
  That headroom holds only while the dispatch module carries the name of its reserve row, `dispatch` (`scripts/cluster/budget.sh:37`), where the dispatch draft names its module `dispatch-api` (`13-dispatch-api.md:91`): with two names the check counts both, and the free figure falls by 256 MiB to 310 MiB, which any step-up of D14 draws on.
- CI cost, estimates: no new workflow, one RabbitMQ container per `just itest` session that needs it, started at about 5 to 10 seconds each, and the regression suite adds about a minute (estimates).
- Estimates, not measurements, in worker-days: lane 0 (3.5: 0.1 (issue) 2, 0.2 (issue) 1.5), S4b (1.5), T.1 (2), C.1 (3), C.2 (2.5), D.1 (2), M.1 (3), exit test (1.5).
  That is about 19 worker-days of effort and about 12.5 days elapsed along 0.1 (issue), 0.2 (issue), T.1, C.1, C.2 and the exit test (2 + 1.5 + 2 + 3 + 2.5 + 1.5), with the proton build on Python 3.13 (Q2), the memory figure of S4b check 10 and cluster lock contention as the main sources of slip.

## 9. Closure

Run on 2026-11-16 against `d0dfedc9ca9455ab1158ebfe340e1243b512ab7b`, the merge of D.1, from a `git archive` of that commit extracted into a scratch directory with no enclosing git repository, recorded by [#420](https://github.com/AndoNorth/weft/issues/420).
Every row passes.
The pasted output of every row is in the pull request that records this run.

Method of the run:

- The worker that ran it wrote none of the lanes.
- The host is the 20 GiB WSL2 VM with Docker running for the whole run, and the one container not started by the run, `open-webui`, was left untouched.
- Row 0's gate is computed from the `ci` preset on `main`, whose `memory_mib` is 12,032 after the simulate slot resize of [#380](https://github.com/AndoNorth/weft/issues/380), so the gate is 13,056 MiB (13,369,344 kB) and not the 12,928 MiB that row 0 states.
- Rows 0 to 8 ran first in order, row 0 was repeated just before row 9, rows 9 to 15 ran in one sequence under the cluster lock, held by one `flock` process with `WFT_CLUSTER_LOCK_HELD=1` on each command, and rows 16 and 17 ran after the lock was released.
- A stopped `wft` cluster of preset `ci`, left by an earlier run with no lock holder, was removed with `just nuke` under the lock before row 9, so row 9 started from no cluster.
- The export has no git checkout, so row 9 runs `scripts/tilt/run.sh ci` with `WFT_BUILD_ID=exit-b`, which the workflow module requires outside one (`deploy/workflow/README.md`).
- The forwards of rows 10 to 15 listen on 127.0.0.1:35672 for AMQP and 127.0.0.1:35692 for the Prometheus endpoint, and were reopened after the pod restart of row 11.
- Row 4's three diagrams were rendered to PNG with ImageMagick and read by eye.

Results:

| # | Result | Evidence |
| --- | --- | --- |
| 0 | pass | `MemAvailable` 18,503,212 kB at the start and 18,498,268 kB before row 9, both above 13,369,344 kB |
| 1 | pass | `uv sync --frozen` exit 0 with `python-qpid-proton==0.40.0` installed, `uv lock --check` exit 0 with 72 packages resolved, the `uv.lock` SHA-256 `9a908f77…` unchanged |
| 2 | pass | no output |
| 3 | pass | exit 0 for the 4 valid vectors and the 3 pinned files, and exit 1 with the `reason:` of its directory for each of the 39 invalid vectors |
| 4 | pass | exit 0 for the three commands, and diagrams 10, 04 and 19 show `standard` as the only class, the claim from `accepted` to `admitted` in place of "workflow id exists", and the four reasons `MALFORMED`, `UNSUPPORTED_VERSION`, `ADMISSION_FAILED` and `delivery_limit` |
| 5 | pass | 155 passed and no `SKIPPED` line, then 2 passed under `-m isolated`, `test_restart_keeps_messages` and `test_held_delivery_across_restart` |
| 6 | pass | 45 passed, every one of the 37 named tests listed `PASSED` |
| 7 | pass | 11 passed |
| 8 | pass | `wft.intake`, `wft.dlx`, `intake.acme.standard`, `intake.dlq`, their two bindings and the policies `wft-intake` and `wft-dlq`, and no user |
| 9 | pass | `cluster wft ready: preset ci, 6 nodes`, `SUCCESS. All workloads are healthy.`, `rabbitmq-0` `Running` in `wft-infra`, `intake.acme.standard` (`wft-intake`) and `intake.dlq` (`wft-dlq`) both `quorum` with 0 messages, and `list_users` lists exactly `admission`, `dispatch` and `operator` |
| 10 | pass | admission's module is not on `main` (no `deploy/admission/`, no admission pod), `S check` prints `assertions: 381/381 passed` and exits 0, and `S count` shows both queues at 0 ready and 0 unacked |
| 11 | pass | `published 20 of 20`, `rabbitmq-0` deleted and ready again, `count` shows `intake.acme.standard ready=20`, and `drain` prints 20 deliveries, each `delivery_count=0` and matching `job.accepted/1.0.json` |
| 12 | pass | four lines, `MALFORMED malformed-json` with `-` for the job, `UNSUPPORTED_VERSION unsupported-major` and `ADMISSION_FAILED no-row` with job `3a9617fa-…`, `delivery_limit` with `-` for the detail and the same job, then one count line per reason |
| 13 | pass | `--limit 1` prints one message line and one count line, `show` prints a message of the job with its annotations, the `delivery_limit` replay replays one, the `MALFORMED` replay refuses with exit 1, the purge removes one, the drain receives the replayed event matching `job.accepted/1.0.json`, and the last list shows the `UNSUPPORTED_VERSION` and `ADMISSION_FAILED` lines only |
| 14 | pass | `rabbitmq_detailed_queue_messages_ready` at 0 for `intake.acme.standard` and 2 for `intake.dlq` |
| 15 | pass | both purges and the forwards closed, exit 0 for `just doctor`, `just down` and both `just nuke`, and after the second `kind get clusters` prints `No kind clusters found.` |
| 16 | pass | `just ci --full` exit 0 in the export, with `--full` because the export has no `origin/main` to pick the path from, 2,769 passed in `just test` and 591 passed in `just itest`, then `all checks passed!` from `nix flake check` |
| 17 | pass | the `ci` run [43859682113](https://github.com/AndoNorth/weft/actions/runs/43859682113) and the `state-modules` run [43859682107](https://github.com/AndoNorth/weft/actions/runs/43859682107), success on `d0dfedc` on `main`; no run on `main` holds this record yet, since it is the pull request that adds it |

Row 13 note: `S deadletter` dead-letters the same golden job for three of its four reasons, so `Q show --job <id>` prints the first message of that job in the queue, the `UNSUPPORTED_VERSION` one, as `show` is documented to do.
`Q show --job <id> --reason ADMISSION_FAILED`, run as a supplement, prints the `ADMISSION_FAILED` message with its detail `no-row: regression suite`, delivery count 6 and its annotations.

Memory figures:

- Host: `MemTotal` 20,482,272 kB, and `MemAvailable` at 18.5 GB before both phases, about 5.1 GB above the gate.
- Broker pod, read after row 14 inside `rabbitmq-0`, which row 11 replaced: `memory.current` 108.1 MiB, `memory.peak` 133.6 MiB with page cache, `anon` 76.9 MiB, of a `memory.max` of 320 MiB, and `rabbitmq-diagnostics alarms` reports no alarm.
- The `ci` infra node: `scripts/cluster/budget.sh ci` prints 5,002 MiB of 5,504 MiB requested, 502 MiB free, with the real `rabbitmq` module at 320 MiB in place of the reserve row.

What was learned:

- The broker holds 20 events across a pod restart on its PersistentVolumeClaim, and the full exchange, queue, policy and user set comes back from the definitions import at boot with no step by hand.
- S4b changed three defaults, all applied by 0.2 (issue) to the broker contract, which is the source: D14 moved to a limit and request of 320 MiB with the watermark at 192 MiB, D6's delayed retry is on (`delayed-retry-type` `failed`, 1 s to 30 s), and D15's consumer count is family `queue_consumer_count`.
  The text of D6, D14 and D15 above keeps the figures the lanes were planned on.
- The 320 MiB step took 64 MiB of section 8's 566 MiB free on the `ci` infra node, which is now 502 MiB, and `02-cluster.md` still quotes 566 MiB.
- Proton 0.40.0 builds from its sdist with the host C compiler, because the dev shell has none of its own (S4b), and the build is cached by `uv`, so row 1 installed it without rebuilding.

What the architecture note now shows: `docs/architecture/10-intake-and-admission.drawio`, `04-intake-dedup-and-dead-letter.drawio` and the new `19-broker-intake-and-dead-letter.drawio`, with their SVGs, are as 0.2 (issue) left them, and were re-checked by eye in this run for row 4.

What moved to which epic, unchanged from section 7 and the rules rows of 0.4:

- publishing from the real submit sequence and reconciler, with `PublishTimeout` and the reconnect (#24)
- consuming with the real admission policy, the start call, `release()` for the four causes of Q4 and the `job.dead.lettered` event (#8), and scaling admission to zero in row 10 once its module is on `main`
- Prometheus scraping of the detailed endpoint, export of the `wft_broker_*` counters and the three alert rules (#13)
- broker chaos under load (#15)
- `max_length`, the caps and the broker footprint at the standard load of 100,000 jobs (#16)
- a broker on more than one node

Follow-ups found:

- [#421](https://github.com/AndoNorth/weft/issues/421): the "State" section of `packages/broker/README.md` still says the client, the topology generator, the T1 container, the suite and the dead-letter tool are not built, which T.1, C.1, C.2 and D.1 each noted as lane 0's file.
- `02-cluster.md` and section 8 above quote 566 MiB free on the `ci` infra node, where `budget.sh ci` now prints 502 MiB, and row 0 above quotes the 12,928 MiB gate of the 11,904 MiB preset; neither changes a check, and the next spec that sizes the infra node or the host takes the figures of this Closure.

Closure addenda, one dated line each, added after close when a `revisit` child issue is filed against this epic:

- none yet
