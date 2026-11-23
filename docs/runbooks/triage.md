# Triage by symptom

Start from what you see, open the dashboard it names, run the query, and follow the link to the alert entry that explains it.
Dashboards and queries need the observability stack, see [the operator runbook](operator.md#observability-on), and the commands for the Prometheus forward, the dead letters and the Temporal CLI are at the top of [alert response](alerts.md#before-you-start).
Without the stack, `kubectl --context kind-wft get pods -A`, the service logs and the Temporal UI still work.

A dashboard is at `http://127.0.0.1:13000/d/<uid>`, with the uid in the first column below.
Queries are run in Grafana Explore on the Prometheus datasource, or at `http://127.0.0.1:19090/graph` after the forward.

## Where a job can wait

A job passes these stages in order, and each symptom below is one stage that does not move.

| Stage | Held by | Where the count is |
| --- | --- | --- |
| Accepted, not yet published | the reconciler of dispatch, then the broker | `wft_dispatch_unpublished_jobs` |
| Published, not yet admitted | admission, its caps and its breakers | `rabbitmq_detailed_queue_messages_ready{queue=~"intake\\..+"}` |
| Admitted, task not yet started | Temporal, then a pool worker | `approximate_backlog_age_seconds{task_type="Activity"}` |
| Running | a pool worker, a task at a time | `wft_worker_tasks_total` by pool and exit class |
| Ended | nothing | `wft_workflow_jobs_total` |

## Symptoms

### Jobs stuck

The job's status does not change, or the job never ends.

1. Read the status: `curl -s http://127.0.0.1:18080/v1/tenants/acme/jobs/<job id>`.
   `accepted` means it is not admitted yet, see [Nothing is admitted](#nothing-is-admitted).
   `admitted` or `running` means it has a workflow, so open it in the Temporal UI at `http://127.0.0.1:18233/namespaces/wft/workflows/job-<job id>`.
2. In the Temporal UI, a task that was scheduled and never started is waiting for a worker: open the `fleet` dashboard, panel "Task attempts by pool /s", and the `placement-pools` dashboard.
   Run `max by (taskqueue) (approximate_backlog_age_seconds{task_type="Activity"})`: a growing value is [WftPoolBacklogAgeHigh](alerts.md#wftpoolbacklogagehigh), and no pollers on the queue is [WftTemporalWorkerUnhealthy](alerts.md#wfttemporalworkerunhealthy).
3. A task that started and keeps failing shows in the `workers` dashboard, panel "Attempts by pool and exit class".
   Compile failures are [WftCompileFailuresHigh](alerts.md#wftcompilefailureshigh), aggregate failures [WftAggregateFailuresHigh](alerts.md#wftaggregatefailureshigh), and memory kills [WftWorkerOOM](alerts.md#wftworkeroom).
4. A status of `paused` is [WftJobPausedNeedsAttention](alerts.md#wftjobpausedneedsattention), and a pause or cancel that does not take effect is [WftControlOutboxOld](alerts.md#wftcontroloutboxold).
5. Heartbeat timeouts with the pool healthy are [WftHeartbeatTimeoutStorm](alerts.md#wftheartbeattimeoutstorm).

For the logs of one job across services, follow [following one job](../../deploy/obs/README.md#following-one-job).

### Nothing is admitted

Jobs sit at `accepted`, or the intake queues grow.

| Open | Query | If it shows | Go to |
| --- | --- | --- | --- |
| `intake-admission`, "Accepted jobs not yet published" | `wft_dispatch_unpublished_oldest_seconds` | a growing age | [WftReconcilerBacklogOld](alerts.md#wftreconcilerbacklogold) |
| `intake-admission`, "Consumers by queue" | `rabbitmq_detailed_queue_consumers{queue=~"intake\\..+"}` | 0 with messages ready | [WftIntakeQueueStalled](alerts.md#wftintakequeuestalled) |
| `intake-admission`, "Breakers open" | `wft_admission_breaker_open` | 1 for a dependency | [WftAdmissionBreakerOpen](alerts.md#wftadmissionbreakeropen) |
| `intake-admission`, "Live jobs against the caps" | `sum(wft_admission_live_jobs)` against `wft_admission_cap_jobs` | at the cap | [WftAdmissionGlobalCapReached](alerts.md#wftadmissionglobalcapreached) and [WftAdmissionOpenCapReached](alerts.md#wftadmissionopencapreached) |
| `intake-admission`, "Broker publishes by outcome /s" | `increase(wft_broker_publish_total{outcome="unroutable"}[10m])` | above 0 | [WftIntakePublishUnroutable](alerts.md#wftintakepublishunroutable) |

With none of these, read the `admission` log: `kubectl --context kind-wft -n wft-control logs deploy/admission`.
The depth of a tenant's intake is `curl -s http://127.0.0.1:18081/v1/tenants/acme/depth`.

### Dead letters

The smoke row `dead-letters` fails, or the dead-letter queue is not empty.

1. List them: `dlq list` from [alert response](alerts.md#before-you-start) prints one line per message and a count per reason.
2. An application reason (`MALFORMED`, `UNSUPPORTED_VERSION`, `ADMISSION_FAILED`) was set by admission, so go to [WftAdmissionDeadLettered](alerts.md#wftadmissiondeadlettered).
   A broker reason (`delivery_limit`, `rejected`, `maxlen`, `expired`) was set by RabbitMQ, so go to [WftDeadLetterQueueNotEmpty](alerts.md#wftdeadletterqueuenotempty).
3. The `fleet` and `intake-admission` dashboards, panel "Dead letters by reason /s", show when they started.

### Missing telemetry

A dashboard is empty, a log line is not in Loki, or a trace is not in Tempo.

1. Open `stack-health`.
   "Replicas available by module" shows a module that is down, and "Restarts by module" and "Memory in use against limit by module" show one that is crashing.
2. Run `up == 0` or open `http://127.0.0.1:19090/targets`: a target that is down is [WftTargetDown](alerts.md#wfttargetdown).
3. Data that was sent and dropped shows in "Dropped logs and spans by component", which is [WftObsDataLoss](alerts.md#wftobsdataloss).
   A full volume stops ingest, see [WftObsVolumeFilling](alerts.md#wftobsvolumefilling).
4. A Loki query that returns nothing is usually a selector that is too wide or a range that is too long: always name one service and keep the range to minutes, see [following one job](../../deploy/obs/README.md#following-one-job).
5. Temporal keeps a closed workflow's history for 24 hours, so a finished job's timeline is gone from the UI after a day while its logs and traces remain.
6. No series at all, and no `WftWatchdog` in the alerts, means Prometheus is down: [WftWatchdog](alerts.md#wftwatchdog).
7. The stack runs only on a cluster created with `WFT_OBS=on`, and `just smoke` prints `observability off, not checked` otherwise.

### Submits fail or the API is not ready

1. `curl -s http://127.0.0.1:18081/readyz` and the same on port 18080: `503` means a database login, the object store or Temporal is down, see [dispatch API readiness](../../deploy/dispatchapi/README.md#readiness).
   A connection reset means the Service has no endpoint yet.
2. The `intake-admission` dashboard, panel "Rejections by reason /s", names the reason code of rejected entries, and the codes are in [the API contract](../../contracts/api/README.md#reason-codes).
3. `TOO_MANY_CALLS` and the other limits are in [the API contract](../../contracts/api/README.md#limits).

### The cluster will not come up, or a pod keeps restarting

1. `nix develop -c just doctor`, then `nix develop -c just ports`, see [the operator runbook](operator.md#bring-up).
2. `nix develop -c just cluster-verify` prints a `FAIL` line for each wrong thing.
3. `kubectl --context kind-wft get pods -A` and `describe pod` on a pod that is not ready.
   A restart is [WftPodRestartUnexpected](alerts.md#wftpodrestartunexpected) or, after an OOM kill, [WftContainerOOM](alerts.md#wftcontaineroom).
4. The `cluster-health` dashboard shows nodes ready, memory against allocatable, and the last termination reasons.
5. `just up` exit 4 is a refusal that names its reason, such as memory or a cluster it did not start.
