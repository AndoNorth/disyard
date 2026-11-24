# Alert response

One entry for every alert rule in [deploy/obs/prometheus/rules/](../../deploy/obs/prometheus/rules/).
Each rule carries a `runbook_url` annotation that points at its heading here, and a test fails when a rule has none or an entry has no rule.
Alerts exist only on a cluster brought up with the observability switch on, see [the operator runbook](operator.md#observability-on).
To go from a symptom to an alert instead, start at [triage by symptom](triage.md).

## Before you start

Every command below runs from the repository root, inside the dev shell (`nix develop`), against the cluster context `kind-dsy`.
The namespaces are `dsy-infra` (database, broker, Temporal, object store), `dsy-control` (dispatch, admission, workflow worker, placement), `dsy-workers` (the three pool workers) and `dsy-obs` (the observability stack).

### Reading the firing alerts

```sh
kubectl --context kind-dsy -n dsy-obs port-forward --address 127.0.0.1 svc/prometheus 19090:9090
```

The forward holds its terminal, so run each forward in its own terminal, or end it with `&`.
Then open `http://127.0.0.1:19090/alerts` for the firing rules and `http://127.0.0.1:19090/graph` for the expression of a rule.
`DsyWatchdog` is always firing, so a list without it means the rules are not being evaluated.

### Tools used in the entries

| What | How |
| --- | --- |
| A dashboard | `http://127.0.0.1:13000/d/<uid>`, where the uid is the file name in [deploy/obs/grafana/dashboards/](../../deploy/obs/grafana/dashboards/README.md) without `.json` |
| A service log | `kubectl --context kind-dsy -n <namespace> logs deploy/<name>`, or in Grafana Explore on Loki `{service_name="<name>"}` with a short time range |
| One job | `http://127.0.0.1:18233/namespaces/dsy/workflows/job-<job id>` in the Temporal UI, and [following one job](../../deploy/obs/README.md#following-one-job) for its logs and traces |
| Job status | `curl -s http://127.0.0.1:18080/v1/tenants/acme/jobs/<job id>` |
| The dead letters | the `dlq` tool below |
| Pool queues and pollers | the Temporal UI (a workflow's task queue page lists its pollers), or the Temporal CLI through a forward of the frontend, below |

The service names in Loki are `dispatch`, `admission`, `workflow`, `placement`, `compile-worker`, `simulate-worker`, `aggregate-worker` and `state-inputs`.

The dead-letter tool needs a forward of the broker and reads the operator password from the state directory:

```sh
kubectl --context kind-dsy -n dsy-infra port-forward --address 127.0.0.1 pod/rabbitmq-0 5672:5672 15692:15692
uv run --frozen python -m dsy_broker.dlq list --address 127.0.0.1:5672 --credentials-dir ~/.local/state/disyard/rabbitmq
```

The directory is `rabbitmq/` under `$DSY_STATE_DIR`, else `$XDG_STATE_HOME/disyard`, else `~/.local/state/disyard`.
`show`, `replay` and `purge` take the same flags, with `--reason CODE`, `--job ID` and `--limit N` to select, and `purge` needs `--yes`.
`replay` refuses a reason that never recovers unless `--force`.

The Temporal CLI is a host tool that the flake does not pin, so install it separately to use it, and every check below has a Temporal UI equivalent.
It needs its own forward:

```sh
kubectl --context kind-dsy -n dsy-infra port-forward --address 127.0.0.1 svc/temporal-frontend 17233:7233
temporal task-queue describe --address 127.0.0.1:17233 --namespace dsy --task-queue <queue>
temporal workflow list --address 127.0.0.1:17233 --namespace dsy --query 'ExecutionStatus="Running"'
```

## Alerts

Severity is the rule's `severity` label.
The rule names its own threshold, and this page never restates it beyond what an entry needs.

| Area | Alerts |
| --- | --- |
| Admission | [DsyAdmissionDeadLettered](#dsyadmissiondeadlettered), [DsyAdmissionBreakerOpen](#dsyadmissionbreakeropen), [DsyAdmissionGlobalCapReached](#dsyadmissionglobalcapreached), [DsyAdmissionOpenCapReached](#dsyadmissionopencapreached), [DsyAdmissionStaleAdmitted](#dsyadmissionstaleadmitted), [DsyJobPausedNeedsAttention](#dsyjobpausedneedsattention), [DsyResumeWaiting](#dsyresumewaiting) |
| Intake | [DsyDeadLetterQueueNotEmpty](#dsydeadletterqueuenotempty), [DsyIntakeQueueStalled](#dsyintakequeuestalled), [DsyIntakePublishUnroutable](#dsyintakepublishunroutable), [DsyIntakeWaitHigh](#dsyintakewaithigh) |
| Dispatch | [DsyReconcilerBacklogOld](#dsyreconcilerbacklogold), [DsyControlOutboxOld](#dsycontroloutboxold) |
| Events | [DsyEventRejected](#dsyeventrejected) |
| Workflow | [DsyScheduleToStartBreached](#dsyscheduletostartbreached), [DsyHistoryLengthHigh](#dsyhistorylengthhigh), [DsyTemporalWorkerUnhealthy](#dsytemporalworkerunhealthy), [DsyOldBuildIdLingering](#dsyoldbuildidlingering) |
| Pools and workers | [DsyPoolBacklogAgeHigh](#dsypoolbacklogagehigh), [DsyUnschedulableTasks](#dsyunschedulabletasks), [DsyHeartbeatTimeoutStorm](#dsyheartbeattimeoutstorm), [DsyPoolSaturated](#dsypoolsaturated), [DsyWorkerOOM](#dsyworkeroom), [DsyCompileFailuresHigh](#dsycompilefailureshigh), [DsyAggregateFailuresHigh](#dsyaggregatefailureshigh) |
| Artifacts | [DsyCompileCacheMissStorm](#dsycompilecachemissstorm), [DsyStoreCapacityHigh](#dsystorecapacityhigh) |
| Cluster | [DsyContainerOOM](#dsycontaineroom), [DsyPodRestartUnexpected](#dsypodrestartunexpected), [DsyTargetDown](#dsytargetdown), [DsyWatchdog](#dsywatchdog) |
| Stack | [DsyObsDataLoss](#dsyobsdataloss), [DsyObsVolumeFilling](#dsyobsvolumefilling) |

### DsyAdmissionDeadLettered

- Means: admission moved intake messages to the dead-letter queue.
  The `reason` label is `MALFORMED`, `UNSUPPORTED_VERSION` or `ADMISSION_FAILED`.
- Confirm: `sum by (reason) (increase(dsy_admission_dead_lettered_total[10m]))` is above 0, and the dead-letter queue is not empty ([DsyDeadLetterQueueNotEmpty](#dsydeadletterqueuenotempty) fires too).
- Check first: the `intake-admission` dashboard, panel "Dead-lettered by admission, by reason", then `dlq list` and `dlq show` for the reason and detail of each message.
  Admission writes no log line when it dead-letters a message, so the log does not name the cause, and a malformed message names no job.
- Resolve, by reason:
  - `MALFORMED` never recovers: `dlq show`, `dlq purge --reason MALFORMED --yes`, and end the named job through the control API (`POST /v1/tenants/acme/jobs/<job id>/cancel` with body `{"reason": "malformed intake message"}`).
  - `UNSUPPORTED_VERSION`: deploy a reader of that major version, then `dlq replay --reason UNSUPPORTED_VERSION`.
  - `ADMISSION_FAILED`: find the cause from the detail token and the admission log, fix it, then `dlq replay --reason ADMISSION_FAILED`.
- The reasons and their recovery are the rows of [contracts/broker/reasons.yaml](../../contracts/broker/reasons.yaml).

### DsyAdmissionBreakerOpen

- Means: the admission breaker of one dependency (the `dependency` label: `temporal`, `database`, `broker` or `internal`) has been open for 5 minutes, and admission starts no job meanwhile.
  A breaker closes on its first good probe, so a long-open one means the dependency is still down, or that distinct messages keep failing with one error class (`internal`).
- Confirm: `dsy_admission_breaker_open == 1`, and the `admission` log names the error.
- Check first: the dependency named by the label:
  - `temporal`: `kubectl --context kind-dsy -n dsy-infra get pods` for the frontend, history, matching and worker pods.
  - `database`: the `appdb` pod and `kubectl --context kind-dsy -n dsy-infra logs statefulset/appdb`.
  - `broker`: `kubectl --context kind-dsy -n dsy-infra get pod rabbitmq-0` and the `intake-admission` dashboard, panel "Consumers by queue".
  - `internal`: the repeated error class in the `admission` log, then the dead letters it caused.
- Resolve: restore the dependency, for a crashed pod see [DsyPodRestartUnexpected](#dsypodrestartunexpected) and [DsyContainerOOM](#dsycontaineroom).
  An `internal` breaker needs the cause of the error fixed, then `kubectl --context kind-dsy -n dsy-control rollout restart deploy/admission` if it does not close.

### DsyAdmissionGlobalCapReached

- Means: live jobs have been at the global cap for 30 minutes, so admission releases no new job until one ends.
  Under a large batch this is the cap doing its work.
- Confirm: the `intake-admission` dashboard, panel "Live jobs against the caps", and `dsy_admission_live_jobs` against `dsy_admission_cap_jobs{scope="global"}`.
- Check first: whether the live jobs are progressing (the `fleet` dashboard, panels "Jobs ended by outcome /s" and "Task attempts by pool /s"), and whether a pool is the bottleneck ([DsyPoolSaturated](#dsypoolsaturated), [DsyPoolBacklogAgeHigh](#dsypoolbacklogagehigh)).
- Resolve: wait when jobs progress.
  When they do not, find the stuck jobs in the Temporal UI and cancel them through the control API.
  The cap is a figure of the tenancy profile in [contracts/tenancy/](../../contracts/tenancy/README.md), and changing it is a design change and not an operation.

### DsyAdmissionOpenCapReached

- Means: open jobs (live plus paused) have been at the open cap for 30 minutes, so admission releases no new job until an open job ends.
- Confirm: `max(dsy_admission_open_jobs)` against `dsy_admission_cap_jobs{scope="open"}`.
- Check first: the `paused-jobs` dashboard, because paused jobs count as open and release nothing by themselves.
- Resolve: resume or cancel paused jobs, see [DsyJobPausedNeedsAttention](#dsyjobpausedneedsattention), or wait for live jobs to end as for [DsyAdmissionGlobalCapReached](#dsyadmissionglobalcapreached).

### DsyAdmissionStaleAdmitted

- Means: admission skipped an intake message because the job row says `admitted` while its workflow is closed.
  Only a terminate from outside the workflow's code leaves a row in that state.
- Confirm: `increase(dsy_admission_skipped_total{reason="stale-admitted"}[10m])` is above 0, and the `admission` log names the job.
- Check first: the job's workflow in the Temporal UI (`job-<job id>`) for who terminated it and when, and the row through the status read.
- Resolve: the job is not running.
  Decide with the job's owner whether it should run.
  Find the cause of the terminate from outside, which is an operator action in Temporal or a change to the namespace.

### DsyJobPausedNeedsAttention

- Means: at least one job has been paused for over an hour.
  A paused job keeps its slot in the open count and its workflow, so it holds capacity until it is resumed or ended.
- Confirm: `sum(dsy_admission_paused_jobs)` and the `paused-jobs` dashboard.
- Check first: who paused the job and whether a patched template was waiting for it, from the job status and the `dispatch` log.
- Resolve: resume it with `curl -s -X POST http://127.0.0.1:18080/v1/tenants/acme/jobs/<job id>/resume -d '{}'`, or end it with the same call and `cancel`.
  A batch is controlled with `.../batches/<batch key>/<action>`.
  A resume waits for a free slot, see [DsyResumeWaiting](#dsyresumewaiting).

### DsyResumeWaiting

- Means: resumes have waited 30 minutes for a free slot, because the caps leave no room for the paused job to continue.
- Confirm: `sum(dsy_admission_resumes_waiting)`, and the `paused-jobs` dashboard, panel "Resumes waiting for a slot".
- Check first: whether [DsyAdmissionGlobalCapReached](#dsyadmissionglobalcapreached) or [DsyAdmissionOpenCapReached](#dsyadmissionopencapreached) fires too.
- Resolve: free a slot by letting or making live jobs end, and the resume proceeds by itself.

### DsyDeadLetterQueueNotEmpty

- Means: the intake dead-letter queue (`intake.dlq`) has held a message for 5 minutes.
  Every dead letter needs a decision, replay or purge.
- Confirm: `dlq list` prints one line per message and a count per reason.
- Check first: the reason and detail of each message, `dlq show`, then the entry for the cause: [DsyAdmissionDeadLettered](#dsyadmissiondeadlettered) for an application reason, and for a broker reason (`delivery_limit`, `rejected`, `maxlen`, `expired`) the table in [contracts/broker/reasons.yaml](../../contracts/broker/reasons.yaml).
- Resolve: `replay` when the reason says `replay` or `after-fix` and the cause is fixed, `purge --yes` otherwise.
  `delivery_limit` means a consumer lost the last delivery, so check admission restarts first.
  `rejected`, `maxlen` and `expired` are not expected on intake, so treat them as a topology or consumer defect.

### DsyIntakeQueueStalled

- Means: an intake queue has ready messages and no consumer for 10 minutes, so nothing admits the jobs of that queue.
- Confirm: `rabbitmq_detailed_queue_consumers{queue=~"intake\\..+"} == 0` with `rabbitmq_detailed_queue_messages_ready` above 0, and `kubectl --context kind-dsy -n dsy-infra exec rabbitmq-0 -- rabbitmqctl list_queues name messages consumers`.
- Check first: the admission pod (`kubectl --context kind-dsy -n dsy-control get deploy/admission`) and `increase(dsy_admission_queue_missing_total[10m])`, which counts tries to consume a queue that the broker does not have.
  The alert needs the broker's own queue series, so with the broker gone it cannot fire, see [DsyAdmissionBreakerOpen](#dsyadmissionbreakeropen) and [DsyReconcilerBacklogOld](#dsyreconcilerbacklogold).
- Resolve: restart or repair admission, see [DsyPodRestartUnexpected](#dsypodrestartunexpected).
  A queue the broker does not have is a topology difference, see [DsyIntakePublishUnroutable](#dsyintakepublishunroutable).

### DsyIntakePublishUnroutable

- Means: the broker client published messages that the exchange could not route, so the topology differs from the tenancy profile of the publisher.
- Confirm: `increase(dsy_broker_publish_total{outcome="unroutable"}[10m])` is above 0, and the `dispatch` log shows the routing key.
- Check first: the queues the broker holds, `kubectl --context kind-dsy -n dsy-infra exec rabbitmq-0 -- rabbitmqctl list_queues name type policy messages`, against the tenants and classes of [contracts/tenancy/](../../contracts/tenancy/README.md).
  The broker imports its topology at boot and never deletes, so a tenant added to a profile needs a pod roll.
- Resolve: render and apply the topology for the profile (the `rabbitmq` module does this on a Tilt run, `scripts/tilt/run.sh ci`), and delete `rabbitmq-0` if it never became ready.
  The accepted jobs that did not publish are retried by the reconciler, see [DsyReconcilerBacklogOld](#dsyreconcilerbacklogold).

### DsyIntakeWaitHigh

- Means: the p95 wait from acceptance to admission of a priority class is over an hour.
- Confirm: the `intake-admission` dashboard, panel "dsy_admission_wait_seconds quantiles".
- Check first: the caps ([DsyAdmissionGlobalCapReached](#dsyadmissionglobalcapreached)), a stalled queue ([DsyIntakeQueueStalled](#dsyintakequeuestalled)) and an open breaker ([DsyAdmissionBreakerOpen](#dsyadmissionbreakeropen)).
- Resolve: the same as the entry that explains it.
  With none firing, the intake simply holds more work than the caps release, and the wait is the queue.

### DsyReconcilerBacklogOld

- Means: an accepted job has waited over 10 minutes for its publish to the broker, so the reconciler is not publishing accepted jobs.
- Confirm: `max(dsy_dispatch_unpublished_oldest_seconds)`, and the `intake-admission` dashboard, panels "Accepted jobs not yet published" and "Oldest unpublished job age".
- Check first: the `dispatch` log for publish errors, the broker (`kubectl --context kind-dsy -n dsy-infra get pod rabbitmq-0`), and a full intake queue (a queue at its length limit refuses publishes, see the "Messages ready by queue" panel).
- Resolve: the reconciler publishes again by itself once the broker accepts.
  Restart `dispatch` with `kubectl --context kind-dsy -n dsy-control rollout restart deploy/dispatch` if it does not, and a resend of a submit is always safe.

### DsyControlOutboxOld

- Means: a control request (cancel, terminate, pause or resume) has waited over 5 minutes for delivery to its workflow.
- Confirm: `max(dsy_control_outbox_oldest_seconds)`, and the `paused-jobs` dashboard, panels "Control requests not delivered" and "Oldest undelivered control request".
- Check first: the `dispatch` log, whose retrier sends the requests, and Temporal reachability (`curl -s http://127.0.0.1:18080/readyz` answers 503 when Temporal is down).
- Resolve: restore Temporal or the workflow worker ([DsyTemporalWorkerUnhealthy](#dsytemporalworkerunhealthy)), and the retrier delivers.

### DsyEventRejected

- Means: the event serializer refused events with the reason in the `reason` label, so a producer emits events that fail the contract.
- Confirm: `sum by (reason) (increase(dsy_event_rejected_total[10m]))`.
- Check first: the logs of the producing services for the rejected event (`dispatch`, `admission`, `workflow`), and the event contract in [contracts/events/](../../contracts/events/README.md).
- Resolve: fix the producer.
  A rejected event is not written, so the row it belonged to may lag its workflow.

### DsyScheduleToStartBreached

- Means: tasks timed out waiting for a worker slot and were placed again.
- Confirm: `sum(increase(dsy_workflow_reschedules_total[15m]))` is above 0, and the `placement-pools` dashboard, panel "Retries and reschedules".
- Check first: the pool whose queue backs up, [DsyPoolBacklogAgeHigh](#dsypoolbacklogagehigh), and that its worker is up (`kubectl --context kind-dsy -n dsy-workers get deploy`).
- Resolve: restore the worker, or wait out a burst.
  Repeated alerts for one pool point at a missing or stalled worker.

### DsyHistoryLengthHigh

- Means: a job workflow woke with a history over 10,000 events.
  The history budget ends a job well before Temporal's hard limit of 51,200 events.
- Confirm: the `characterization` dashboard, histogram `dsy_workflow_history_events`, and the Temporal UI event count of the job.
- Check first: the job's template, since a job with very many tasks or retries grows its history.
- Resolve: end the job and submit it again in smaller batches.
  The history budget ends a job that keeps growing.

### DsyTemporalWorkerUnhealthy

- Means: no worker polls a Temporal task queue (the `task_queue` label), so tasks on it wait.
  The worker exports its metrics but has no poller.
- Confirm: the queue shows no pollers (`temporal task-queue describe`, or the task queue page of a workflow in the Temporal UI), and `kubectl --context kind-dsy -n dsy-control get deploy`.
- Check first: the log of the owning service (`workflow-worker-<build id>` for `dsy-workflow` and its control queue, `<pool>-worker` in `dsy-workers` for a pool queue), and Temporal's own pods.
  A worker Deployment scaled to zero removes its queue's series instead of setting it to 0, so the alert does not fire for it: read `kubectl --context kind-dsy -n dsy-workers get deploy` and the `placement-pools` panel "Workers available by pool".
  When the Temporal frontend is down, the alert fires for Temporal's own queues (`default_worker_tq` and `temporal_sys_*`) and not for the queues of the platform.
- Resolve: restart the deployment (`kubectl --context kind-dsy -n dsy-control rollout restart deploy/workflow-worker-<build id>`).
  A fresh cluster has no current build, so the first job waits until `just smoke` or the workflow harness makes the deployed build current.

### DsyOldBuildIdLingering

- Means: a workflow build (a `workflow-worker-<build id>` Deployment) still runs 7 days after a newer build appeared.
  A build lives until its jobs finish and no job lasts over 7 days, so a job is stuck on it.
- Confirm: `kubectl --context kind-dsy -n dsy-control get deploy --sort-by=.metadata.creationTimestamp` lists two or more `workflow-worker-<build id>` Deployments, and `kube_deployment_created` ranks them.
- Check first: the open workflows pinned to the old build, the Temporal UI workflow list filtered to running workflows (or `temporal workflow list --query 'ExecutionStatus="Running"'`), oldest first.
- Resolve: end or fix the stuck job.
  Delete the old Deployment only once no job is pinned to it, because a job finishes on the build that started it.

### DsyPoolBacklogAgeHigh

- Means: the oldest activity task on a Temporal queue (the `taskqueue` label) has waited over 10 minutes, so the pool is not keeping up.
  The server spells the queue with underscores in place of dashes.
- Confirm: `approximate_backlog_age_seconds{task_type="Activity"}`, and the `placement-pools` dashboard.
- Check first: whether the pool worker is up and polling ([DsyTemporalWorkerUnhealthy](#dsytemporalworkerunhealthy)), and whether the pool is saturated ([DsyPoolSaturated](#dsypoolsaturated)).
  A pool runs one task at a time per node, so a backlog under load is expected.
  A pool with no worker at all does not raise the alert, because the backlog age stayed at 0 with a task waiting for 17 minutes, so check `kubectl --context kind-dsy -n dsy-workers get deploy` as well.
- Resolve: wait when the pool is busy and healthy, otherwise restore the worker.
  Pool width is the number of pool nodes of the preset, and the `soak` preset has two compile and two simulate nodes against one each in `ci`, see [the operator runbook](operator.md#presets-and-memory).

### DsyUnschedulableTasks

- Means: placement found no slot for tasks of a pool (the `pool` label), because a task needs more than any slot of the pool offers.
- Confirm: `increase(dsy_placement_decisions_total{verdict="unschedulable"}[15m])`, the `placement-pools` dashboard, and `kubectl --context kind-dsy -n dsy-control exec deploy/placement -- python -m dsy_placement.show`.
- Check first: the task's `cpu` and `memory_mib` in the job template against the pod slot of the pool in [deploy/presets/slots/](../../deploy/presets/slots/), and the `placement` log lines `unschedulable ...`.
- Resolve: lower the request in the template.
  The pod slots are the same on every preset, so a larger slot is a change of the slot file and of the node sizes, which is a design change and not an operation.

### DsyHeartbeatTimeoutStorm

- Means: more than 5 attempts on a queue timed out waiting for a heartbeat in 15 minutes, so workers are stalled or lost and the attempts are retried.
- Confirm: `sum by (taskqueue) (increase(activity_task_timeout{timeout_type="Heartbeat"}[15m]))`, and the `workers` dashboard, panel "Heartbeat lag p95 by pool".
- Check first: the pool worker pod for restarts and OOM kills (`kubectl --context kind-dsy -n dsy-workers get pods`, then [DsyContainerOOM](#dsycontaineroom)), node CPU throttling on the `cluster-health` dashboard, and the worker log.
- Resolve: restart the worker deployment, and fix the cause the log names.
  A node starved of CPU needs a larger preset, and a task that never heartbeats needs its own fix.

### DsyPoolSaturated

- Means: every activity slot of a queue has been busy for 30 minutes.
  This is information: the pool runs at full width, which is expected under load.
- Confirm: `temporal_worker_task_slots_used` against `temporal_worker_task_slots_available` for the queue.
- Check first: the backlog on the same queue ([DsyPoolBacklogAgeHigh](#dsypoolbacklogagehigh)).
- Resolve: nothing, unless the backlog grows without end.

### DsyWorkerOOM

- Means: task attempts of a pool were killed for memory (`oom_killed`, `compile_oom` or `aggregate_oom`), so the task needs more memory than its pool slot gives it.
- Confirm: `sum by (pool) (increase(dsy_worker_tasks_total{exit_class=~"oom_killed|compile_oom|aggregate_oom"}[15m]))`, and the `workers` dashboard, panels "OOM attempts per pool" and "Worker memory by pool".
- Check first: the memory the task asks for in the template against the pool slot, and the guest memory cap.
- Resolve: ask for less memory in the template.
  The pod slots are the same on every preset, so a larger slot is a design change.
  The pods themselves being OOM-killed is [DsyContainerOOM](#dsycontaineroom).

### DsyCompileFailuresHigh

- Means: more than 5 compile attempts ended in the same failure class (`exit_class`) in 15 minutes.
  Look at the class before the testbench: `compile_dependency_unavailable` points at the package mirror, `compile_failed` at the source.
- Confirm: `sum by (exit_class) (increase(dsy_worker_tasks_total{pool="compile", exit_class!~"ok|compile_oom"}[15m]))`, and the `workers` dashboard, panel "Attempts by pool and exit class".
- Check first: the `compile-worker` log, and the package mirror pod for `compile_dependency_unavailable` (the mirror runs in the `ci` preset only).
- Resolve: fix the source for `compile_failed`, restore the mirror for an unavailable dependency, or raise the timeout for `compile_timeout`.

### DsyAggregateFailuresHigh

- Means: more than 5 aggregate attempts ended in the same failure class in 15 minutes.
  Look at the class before the results: `aggregate_input_unavailable`, `aggregate_input_corrupt` and `aggregate_input_refused` point at the object store, `aggregate_parse_failed` at the simulate result.
- Confirm: `sum by (exit_class) (increase(dsy_worker_tasks_total{pool="aggregate", exit_class!~"ok|aggregate_oom"}[15m]))`.
- Check first: the `aggregate-worker` log, and the object store ([DsyStoreCapacityHigh](#dsystorecapacityhigh), `kubectl --context kind-dsy -n dsy-infra get pod objectstore-0`).
- Resolve: restore the store for the input classes, and fix the producing simulate task for a parse failure.

### DsyCompileCacheMissStorm

- Means: over 90 percent of the compiles of the last hour ran instead of hitting the cache, so the compile cache is not being reused and a key may vary between identical sources.
- Confirm: `delta(dsy_state_cache_misses[1h])` and `delta(dsy_state_cache_hits[1h])`.
- Check first: whether the jobs really share a source (a fresh cluster or a new source version misses once, which is not a defect), and the cache key inputs of `dsy_keys.compile_cache_key` in [packages/keys](../../packages/keys/README.md).
- Resolve: find the input that varies between identical sources, such as a timestamp in the bundle, and remove it.

### DsyStoreCapacityHigh

- Means: the object store data volume is over 70 percent used.
- Confirm: the `SeaweedFS_volumeServer_resource` series in the alert, and the `stack-health` dashboard for the volume.
- Check first: which buckets hold the bytes, and whether the lifecycle worker is alive (the pod reads `3/3` ready, `2/3` means a dead worker, see the runbook of the store).
- Resolve: follow [the object store runbook](../../deploy/objectstore/README.md#runbook): delete objects with the `reaper` identity, vacuum, and put an object to confirm, and restart the pod for a dead lifecycle worker.

### DsyContainerOOM

- Means: a container was OOM killed (last exit 137 with reason `OOMKilled`) or waits on memory over 5 percent of the time, which is a container at its limit or a node short of memory.
- Confirm: `kubectl --context kind-dsy get pods -A` for restarts, the `cluster-health` dashboard panels "Last termination reasons" and "Memory in use against allocatable by node", and `kubectl --context kind-dsy -n <namespace> describe pod <pod>`.
- Check first: which container and which namespace.
  A pool worker in `dsy-workers` is workload behaviour, see [DsyWorkerOOM](#dsyworkeroom).
  A control or infra pod at its limit needs its limit raised in its `module.yaml`, within the budget `just cluster-verify` checks.
- Resolve: raise the limit and roll the pod, or make the preset larger.
  Limits are fixed at node creation for the node itself, see [the operator runbook](operator.md#presets-and-memory).

### DsyPodRestartUnexpected

- Means: a container restarted in a `dsy` namespace and the cause was not an OOM kill, which [DsyContainerOOM](#dsycontaineroom) reports.
- Confirm: `kubectl --context kind-dsy get pods -A` shows the restart count.
- Check first: `kubectl --context kind-dsy -n <namespace> logs <pod> --previous`, and `describe pod` for the last state and its reason.
- Resolve: fix what the previous log shows.
  A restart after a `just up` following a host sleep is expected, see [the operator runbook](operator.md#host-sleep-and-restart).

### DsyTargetDown

- Means: Prometheus cannot scrape a target (the `job` and `instance` labels) for 5 minutes, so series and alerts that depend on it are blind.
- Confirm: `http://127.0.0.1:19090/targets` after the forward, and `up == 0`.
- Check first: the target's pod, `kubectl --context kind-dsy -n <namespace> get pods`, and whether a NetworkPolicy blocks Prometheus (worker pools allow ingress from `dsy-obs` on port 9464 only with the switch on).
- Resolve: restore the pod.
  The alert covers a pod that exists and cannot be scraped.
  A deployment scaled to zero has no target, so nothing fires for it and `up` has no series: find it with `kubectl --context kind-dsy get deploy,statefulset -A`, and for the observability modules the `stack-health` panel "Replicas available by module".

### DsyWatchdog

- Means: the alerting pipeline is alive.
  The rule always fires, and its absence means rules are not evaluated.
- Confirm: it is listed at `http://127.0.0.1:19090/alerts`.
- Check first: when it is missing, `kubectl --context kind-dsy -n dsy-obs get pods` for Prometheus, and `promtool check rules` through `nix develop -c uv run --frozen pytest packages/obs/tests/rules`.
- Resolve: nothing when it fires, and restart or repair Prometheus when it does not.

### DsyObsDataLoss

- Means: a component (the `component` label) dropped log records or spans from a full export queue, so the collector or a backend behind it is not keeping up and the missing telemetry is gone.
- Confirm: `increase(dsy_obs_log_dropped_total[15m])` and `increase(dsy_obs_span_dropped_total[15m])`, and the `stack-health` dashboard, panels "Dropped logs and spans by component" and "Collector records accepted and refused".
- Check first: the collector, Loki and Tempo pods in `dsy-obs` for restarts and memory (`stack-health`, "Memory in use against limit by module"), and [DsyObsVolumeFilling](#dsyobsvolumefilling).
- Resolve: restore the backend that is slow or down, and reduce the log volume of the component if it floods.
  Lost records are not recovered.
  The counters only rise when an export queue overflows, so a collector that is gone (scaled to zero, for example) loses its records with no count and no alert: the `telemetry` row of `just smoke` fails and the `stack-health` panel "Replicas available by module" reads 0 for the collector.

### DsyObsVolumeFilling

- Means: a backend (the `backend` label: `prometheus`, `tempo` or `loki`) holds more than its size cap.
  Loki and Tempo have no size key, so this alert is their only brake on a flood.
- Confirm: the `stack-health` dashboard, panels "Volume use by module" and "Volume bytes used by module".
- Check first: what writes so much: the `stack-health` ingest panels ("Loki bytes received", "Tempo spans received", "Prometheus head series") name the backend's input.
- Resolve: stop the flood at its source.
  The data of the stack ends with `just nuke`, which is the last resort for a full volume.
