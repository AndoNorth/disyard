# Epic #13: Observability - breakdown

Status: draft, awaiting spec review
Epic: https://github.com/AndoNorth/wasm-dag-forge/issues/13
Architecture note: `docs/architecture/05-observability-data-flow.drawio`, refined in place by 0.2 (issue) to show the shape this spec builds (pull scrape, one collector for logs, events and traces, Tempo and Pyroscope, the whole stack behind the observability switch)
Depends on (closed epics): #1, #2, #4, #5, #3, #12, #11, #6, #9, #10, #7, #8, #24, and pass 1 of #17 (the smoke gate) merged and passed

Evidence marks: `[read]` means read on `main` at `ef5ca98` on 2026-11-19 while preparing this spec, with read-only commands and no runtime.
Where the epic body and landed code or a pinned contract disagree, the landed code and the contract win, and the row that applies says so.
Tiers as in the sibling specs: T0 host Python with no runtime, T1 containers through `wdf_itest`, T2 the cluster from `just up` plus `scripts/tilt/run.sh ci` on preset `ci`.
Every command takes `nix develop -c`, every Python command also takes `uv run --frozen`, and every T2 command runs under `flock /tmp/wdf-cluster.lock env WDF_CLUSTER_LOCK_HELD=1 ...`.
"Switch on" means a cluster brought up with `WDF_OBS=on` (D19), and "switch off" means one brought up without it.
Figures marked "estimate" are proposals that a spike, a test or #16 replaces.

## 0. Decisions

Section 0.1 lists what is already decided, 0.2 the open questions, 0.3 the defaults the lanes build on, and 0.4 the proposals that lane 0 and the issues pin.
"0.1 (issue)" and "0.2 (issue)" name the lane 0 issues of section 4, and a bare 0.1 to 0.4 names a part of this section.

### 0.1 Settled decisions

Everything in the epic body stands except where a built epic, a pinned contract or a captain decision has overtaken it, which this table and section 1 list.

| # | Decision | Source |
| --- | --- | --- |
| 1 | The contract comes before the stack: bounded labels only, never a job, task, attempt, event or trace id in a label | epic key decisions; `contracts/instrumentation/README.md:18-24` |
| 2 | A catalogue plus a contract test: every metric a dashboard or rule uses is in the catalogue, every catalogue entry shows in a live scrape during acceptance, and no catalogue label is denied | epic key decisions |
| 3 | Plain Prometheus plus kube-state-metrics, no operator, one replica, rule files from a ConfigMap, size and time retention caps | epic key decisions |
| 4 | Loki in monolithic mode, retention switched on, correlation ids in structured metadata and never in index labels | epic key decisions |
| 5 | Alerts are Prometheus rule files checked by `promtool check rules` and `promtool test rules`, shown in Grafana, with no Alertmanager; harnesses read `ALERTS` from the Prometheus API | epic key decisions |
| 6 | No per-job dashboard and no job UI | epic Goal and key decisions |
| 7 | OOM from exit 137 and node events, not from `OOMKilled` | epic key decisions; `02-cluster.md:65` (topology rule) |
| 8 | Nothing outside `obs` depends on an `obs` resource, and every obs module is group `obs` in `wdf-obs` | `02-cluster.md:64`; #2 Interfaces row |
| 9 | The broker's three alert rules are installed as stated, by name and expression: `WdfDeadLetterQueueNotEmpty`, `WdfIntakeQueueStalled`, `WdfIntakePublishUnroutable`; depth comes from `/metrics/detailed` family `queue_coarse_metrics` and consumers from `queue_consumer_count` on port 15692 | `contracts/broker/README.md:134-136`, `10-broker.md:122,256`, S4b result |
| 10 | This epic exports the `wdf_events.metrics` registry from every process that loads it, dispatch and admission included | `10-broker.md:122,256,368`, `12-admission.md:149`, `13-dispatch-api.md:97` |
| 11 | The admission names of its D14, served on 9464, and the alerts its rules row names (a dead letter, a breaker open over 5 minutes, live jobs at the global cap and open jobs at the open cap over 30 minutes, a `stale-admitted` skip), all thresholds estimates | `12-admission.md:149,185` |
| 12 | The dispatch names of its D15, served on 9090, and the alerts `ReconcilerBacklogOld` and `ControlOutboxOld` | `13-dispatch-api.md:97,227` |
| 13 | The workflow names of its rules row on 9464, the SDK metrics, and the Temporal `cache_usage` series on port 9090 of the history pod; this epic owns the alert rules on them, the history length alert included | `08-workflow-engine.md:301`, `docs/delivery/spikes/S10/helm-install.md:96` |
| 14 | Placement metrics are this epic's to define and add; the decision log line stays; "build id per pod" is the worker image per pod | `14-placement.md:68,73,99` |
| 15 | Worker metrics, the SDK metrics port and JSON logs in `wdf_worker.service` are this epic's | `11-workers.md:76,106` |
| 16 | The metric names and exporters of the state inputs are this epic's; artifact failures come from the input `artifact_failures` by code | `05-state-storage.md:89,161`, `06-artifact-passing.md:504` |
| 17 | This epic adds the telemetry row to `just smoke` as part of its exit; the gate is `ci` only | `17-acceptance-smoke.md:29,48,60` |
| 18 | Trace context: the `traceparent` born at submit, a span per handled event, no promise of one trace for a job that lasts days | `contracts/instrumentation/README.md:34-39`, `13-dispatch-api.md:117`, `12-admission.md:42` |
| 19 | A worker logs outcomes and never one line per guest heartbeat; the guest output tail (64 KiB) is stored as `log-tail` and Loki owns the full service logs | epic key decisions, `06-artifact-passing.md:111,507` |
| 20 | Starting retention, to be measured: Prometheus 7 days and 4 GB, Loki 7 days and 8 GB, Tempo 3 days and 3 GB, Pyroscope 3 days and 3 GB | epic key decisions |
| 21 | No verifier skill; every issue names its tests and the exit test is a command checklist | `docs/delivery/method.md:149-154` |
| 22 | Q1: the observability stack is an opt-in switch on the `ci` preset that local runs and the smoke gate use. A local run or a smoke deploy turns it on, and with it the larger obs node that S13 sizes; GitHub CI jobs never deploy it. The mechanism is D19. Why: "observability stack should only be upped when testing locally or doing smoke tests, not in the CI right now" | captain, 2026-11-19 |
| 23 | Q2: logs reach Loki by OTLP push from each Python service to one collector Deployment on the obs node; no collector runs on a pool node. Why: a `filelog` DaemonSet does not fit the pool nodes (`slots.sh ci` leaves 22 MiB and 50m on the simulate node [read]), and every log line that carries a job id comes from a Python service | captain, 2026-11-19 (option a) |
| 24 | Q3: Tempo and Pyroscope are built in this pass, behind the same switch as the rest of the stack, for local, smoke and load runs and never in GitHub CI. S13 sizes the obs node for the whole stack including them, and the deferral of the draft is replaced by lane P and by trace and profile wiring in lanes H and E. Why: "i want these, but for the load and smoke type testing not in the CI" | captain, 2026-11-19 |
| 25 | Q4: the log fields are renamed to the epic body's names, `ts`, `level`, `component`, `msg`, `job_id`, `task_id`, `attempt_id`, `trace_id`, `span_id`, as an owned change of the instrumentation contract to version 3 that migrates every emitter, not as an additive extension. Why: the body's names read better in log queries and in dashboards | captain, 2026-11-19 (option b) |
| 26 | Q5: no Temporal UI in this pass. Grafana links a Loki query by `job_id` and the control status route, and the per-job timeline is `temporal workflow show --workflow-id job-<job id>` through a forward. Why: the infra node is the scarcest room on the host, and the log query by job id already answers "what happened to this job" | captain, 2026-11-19 (option a) |

### 0.2 Questions for the captain

None open: Q1 to Q5 are settled as decisions 22 to 26.

### 0.3 Defaults the lanes build on

| # | Topic | Default | Reason, and the alternative | Changes |
| --- | --- | --- | --- | --- |
| D1 | Discovery | One `kubernetes_sd` pod job that keeps a pod with the annotation `prometheus.io/scrape: "true"` (the Temporal chart's, port from `prometheus.io/port`) or a container port named `metrics` or `prometheus` (admission, dispatch, workflow, object store, RabbitMQ, and the ports this epic adds); a second RabbitMQ job on `/metrics/detailed?family=queue_coarse_metrics&family=queue_consumer_count`; cAdvisor and kubelet metrics scraped on each node's port 10250 with Prometheus's service account token, not through the API server proxy; interval 30 s (estimate) | The built modules already name their port `metrics` (`deploy/admission/Tiltfile.module:95`, `deploy/dispatchapi/Tiltfile.module:100`, `deploy/workflow/Tiltfile.module:90`, `deploy/objectstore/Tiltfile.module:115`), so no closed module gains an annotation. The alternative, annotations everywhere as the epic says, edits five closed modules. Direct kubelet scrapes keep load off the 1-CPU control plane, which already fails liveness on fresh clusters (https://github.com/AndoNorth/wasm-dag-forge/issues/528) | S.1 |
| D2 | Node health base | cAdvisor, kube-state-metrics and `kube_node_status_allocatable`; no node-exporter | The epic notes node-exporter on kind may report host totals, and #2 made allocatable truthful | S.1 |
| D3 | Third-party series in the catalogue | `metrics.yaml` v3 gains an `external` map: each third-party series a dashboard or rule reads, with its scrape job and the labels the rules group by; it is exempt from the `wdf_` naming rule and held to the label denylist; the live scrape test covers it | The catalogue rule ("every metric used in a dashboard or rule must exist in the catalogue") cannot hold Temporal, RabbitMQ, kube-state-metrics, cAdvisor, Tempo or Pyroscope names under the `wdf_` rule. The alternative, recording rules that rename each one, doubles the series | 0.1 (issue), C.1 |
| D4 | Alert names and severity | Every alert takes the `Wdf` prefix of the pinned broker rules (so the epic's `DlqGrowing` is `WdfDeadLetterQueueNotEmpty`), and a `severity` label of `critical`, `warning` or `info`; `WdfPoolSaturated` is always `info`; the max-load severity mode of the epic's risks is left to #16, which runs max load | One naming rule for the three pinned rules and the rest. A severity mode needs a load signal that only #16 runs | A.1, A.2 |
| D5 | Paused gauge | `wdf_admission_paused_jobs` is the one paused gauge, as #8 left the call here; `wdf_workflow_paused_jobs` and `wdf_workflow_replay_failures_total` are not catalogued, because nothing emits them, and Closure names them as #6's leftovers | A catalogue entry with no emitter fails decision 2 at once | 0.1 (issue) |
| D6 | Characterization buckets | The buckets of every characterization histogram are pinned in `metrics.yaml` v3 now, and #16 changes them only by a contract-change PR | The epic says they are "fixed and documented by #16", but the emitters need buckets before #16 runs. Admission and dispatch already pass buckets through `histogram_bucket_overrides` (`packages/admission/src/wdf_admission/service/metrics.py:40-43`) | 0.1 (issue), E.2 to E.6 |
| D7 | Postgres datasource | None (the epic's open question 1): Loki by `job_id` plus the control status route cover the drill-down | A datasource needs another Grafana dependency and a login, for a question the status route answers | S.3 |
| D8 | Grafana access | `127.0.0.1:13000` to NodePort 30300 through a new row of `deploy/host/ports.yaml` (R.2); anonymous Viewer; the admin password generated into the state directory on first use, held in a Secret, never committed; datasources and dashboards provisioned from ConfigMaps | The epic's #2 row asks for a 127.0.0.1 endpoint. The port row is rendered on every `ci` cluster, switch on or off, because ports are fixed at `just up`, and with the switch off nothing answers behind it | R.2, S.3 |
| D9 | Stack figures | Memory requests equal limits (estimates, set by S13): Prometheus 384 MiB and 200m, Loki 256 MiB and 100m, Tempo 256 MiB and 100m, Pyroscope 256 MiB and 100m, Grafana 128 MiB and 50m, collector 128 MiB and 50m, kube-state-metrics 64 MiB and 20m, state inputs exporter 96 MiB and 20m; 1,568 MiB and 640m in all, against 1,854 MiB and 1,300m of requests on a 2,304 MiB, 1.5-CPU obs node (allocatable 1,904 MiB by the reservation rule of `contracts/preset/README.md`, less 50 MiB and 100m of daemons) | Requests equal to limits keep the obs node from overcommitting, and the free 286 MiB is the margin S13 must confirm. The collector is larger than a logs-only one because it also carries traces and events | S13, R.2, S.1 to S.3, P.1, P.2, I.1 |
| D10 | Images and manifests | Plain manifests rendered in each `Tiltfile.module`, no chart; each image pinned by digest in `deploy/versions.yaml` at the newest release on the day its issue starts, one key per line; one module per component under `deploy/obs/<component>/` | The module contract allows an image pin, and the Temporal chart is the only chart in the repository for a reason (its size) that does not apply here | S.1 to S.3, P.1, P.2, I.1 |
| D11 | Obs volumes | Prometheus, Loki, Tempo and Pyroscope keep their data in local-path volumes on the obs node, which live inside the node container and end with `just nuke`, because only the infra node mounts the host data directory (`scripts/cluster/render.sh:145-149`); soak evidence is exported before a nuke, as the epic says | Moving obs data to the host would mount the data directory on a second node, a #2 change for data the PoC can lose | S.1, S.2, P.1, P.2 |
| D12 | State inputs exporter | `python -m wdf_obs.inputs`, module `state-inputs` in `wdf-obs`, one login in `wdf_reader`, runs the `sql`, `sql_labelled` and `invariants` inputs every 60 s (estimate) and serves `wdf_state_<name>` gauges on 9464 named `metrics`; the two `store` inputs need `kubectl exec` into the store pod (`packages/stateops/src/wdf_stateops/inputs.py:35,135-141`), so store bytes come from SeaweedFS's endpoint on 9327 and volume use from kubelet volume stats | An in-cluster exporter with exec rights on the store pod would be the only pod with that grant | I.1 |
| D13 | Metric transport | Every Python process serves its names through its Temporal runtime's Prometheus endpoint, as admission, dispatch and workflow already do; placement and the exporter do the same, and `wdf_obs` adds no `prometheus_client` dependency | One exposition path, already proven in three services. Metrics are served with the switch off too, because a metrics port costs nothing and nothing scrapes it | H.2, E.2 to E.6, I.1 |
| D14 | Log transport (decision 23) | `wdf_obs.logs` installs one handler that writes the JSON line to stdout and, when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, queues the same record for an OTLP log exporter (HTTP, batched, queue capped at 2,048 records with drops counted as `wdf_obs_log_dropped_total`, estimates) whose failure never blocks or fails the service; resource attribute `service.name` is the component; the collector adds no labels beyond Loki's OTLP defaults, so correlation fields land in structured metadata | Decision 4, and the module rule that nothing depends on an obs resource at runtime. With the switch off the endpoint is unset (D20), so a service writes stdout only | H.1, S.2 |
| D15 | Log fields (decision 25) | `ts` (RFC 3339 UTC), `level`, `component`, `msg`, and when known `tenant`, `job_id`, `task_id`, `attempt_id`, `trace_id`, `span_id`, `event_type`, `cloudevent_id`, `pool`, `exit_class`. The context attribute maps to a log field as `jobid` to `job_id`, `taskid` to `task_id`, `attempt` to `attempt_id`, and `traceparent` to `trace_id` and `span_id`; `traceparent` itself is no longer a log field. Span attributes keep the context attribute names, as the contract's Traces section says | The rename of decision 25 is about log lines; spans carry the event's own attribute names, and Grafana links logs to traces by `trace_id`, not by job id. Renaming span attributes too would change #4's trace rule for no query this epic writes | 0.1 (issue), H.1, E.2 to E.6 |
| D16 | The telemetry row of `just smoke` | Row 11 `telemetry`, after row 10. With the switch off it prints `skip telemetry: observability off` and fails nothing. With the switch on, over forwards to `svc/prometheus:9090`, `svc/loki:3100`, `svc/tempo:3200` and `svc/pyroscope:4040` in `wdf-obs`: every `up` series is 1 and the set of `job` labels equals `wdf_obs.targets.JOBS`; Loki answers at least one line with `job_id` equal to row 6's job id from each of `dispatch`, `admission`, `workflow`, `compile-worker`, `simulate-worker` and `aggregate-worker`; Tempo returns the trace of row 6's `trace_id` with spans whose `service.name` covers `dispatch`, `admission` and `workflow`; Pyroscope lists a profile in the last 5 minutes for each of `dispatch`, `admission`, `workflow`, `placement` and the three worker components. Each part is retried every 2 s up to 60 s or the remaining budget less 30 s, whichever is less; the row runs when rows 1 and 6 were ok, else prints `skip` | The smoke row as #17's body words it, bounded as the gate's other rows are (`17-acceptance-smoke.md:53`, D6), covering every signal the switch turns on. A gate that a switch-off cluster cannot pass would stop pass 2 of #17 from ever running the gate in CI, which never turns the stack on. Placement has no row in the log check because its request carries no job id (`14-placement.md:68`) | T.1 |
| D17 | Third-party logs | Under decision 23, Temporal, RabbitMQ, Postgres and the store keep `kubectl logs` only; Kubernetes events of every namespace go to Loki through the collector's event watch, so node and pod events are queryable | The events carry the node signal of decision 7 | S.2 |
| D18 | Package | `packages/obs/`, import `wdf_obs`, as the epic names it | | 0.2 (issue) |
| D19 | The observability switch (decision 22) | `WDF_OBS=on` at `just up` turns the stack on for that cluster, and its absence (or `off`) leaves it off; `off` is the default. The preset contract gains an optional `obs_stack: {cpus, memory_mib}`, the obs node when the switch is on, set on `ci` and `soak` (D9's 2,304 MiB and 1.5 CPU, estimates set by S13) and absent on `dev`, where `WDF_OBS=on` is a usage error. `deploy/tilt/profiles.yaml` moves `obs` out of `groups` into a new `opt_in: [obs]` on `ci` and `soak`: a group in `opt_in` is enabled only with the switch on. `up.sh` records the value in the state directory as `cluster.obs` beside `cluster.owner`, refuses (exit 4) an existing cluster recorded with the other value, and refuses (exit 2) `WDF_OBS=on` when `GITHUB_ACTIONS` is `true`; `nuke.sh` removes the record. Every reader of the obs node or of the profile's groups applies the switch: `render.sh`, `namespaces.sh` and `budget.sh` from `WDF_OBS` (they run before or without a cluster), and `scripts/tilt/run.sh`, `cluster-verify.sh` and the smoke driver from the record, so one value set at `just up` governs the cluster | The obs node size is fixed when kind creates the node, so the switch has to be a `just up` choice, and recording it keeps a later `run.sh`, `cluster-verify` or `just smoke` from silently disagreeing with the node that exists. The alternatives, a fourth preset or a `ci` obs node that is always 2,304 MiB, either break the `ci`-only rule of the gate (`17-acceptance-smoke.md:48`, D1) or give CI a larger node it never uses, against decision 22's "and with it the larger observability node" | R.1, R.2, S13, T.1 |
| D20 | Wiring the services to the stack | The Tiltfile exports `WDF_OBS` to the module Tiltfiles, as it exports `WDF_LIVE_UPDATE` (`Tiltfile:28`), and a shared `deploy/tilt/obs.star` gives `obs_env()`: with the switch on, `OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.wdf-obs:4318` and `WDF_PYROSCOPE_ADDRESS=http://pyroscope.wdf-obs:4040`, and with it off, nothing. Each emitting module adds `obs_env()` to its container env | One place decides what a service sends, and a switch-off service never resolves an obs name, which keeps decision 8 true at runtime and not only by convention | R.1, E.2 to E.6, I.1 |
| D21 | Trace export (decision 24) | `wdf_obs.traces` installs an SDK `TracerProvider` with resource `service.name` equal to the component, a `BatchSpanProcessor` over the OTLP HTTP span exporter to the collector (queue capped at 2,048 spans, drops counted as `wdf_obs_span_dropped_total`, estimates), and the `ParentBased(TraceIdRatioBased(1.0))` sampler, only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set; with it unset no provider is installed and spans stay no-ops, as today. The collector forwards traces to Tempo's OTLP receiver | The spans already exist (`packages/temporal/src/wdf_temporal/trace.py:31-41`, `13-dispatch-api.md:117`) and only lack a provider. Sampling every trace fits smoke and the first soak; the ratio is a setting that #16 lowers if 100,000 jobs per batch needs it, as the epic's risks say | H.3, S.2, P.1 |
| D22 | Profiles (decision 24) | Pyroscope push mode from the Python SDK (`pyroscope-io`): `wdf_obs.profiles` calls `pyroscope.configure` with application name `wdf.<component>`, the SDK's default sample rate, and tags `pool` where the component has one, only when `WDF_PYROSCOPE_ADDRESS` is set. Every Python service that calls `wdf_obs.setup` is profiled, which adds dispatch to the epic's four | Push mode in the Python services is the epic's key decision; profiling dispatch costs one more application name and no more code, because one setup call covers every service | H.3, P.2 |
| D23 | One setup call | `wdf_obs.setup(component)` installs logs (D14), traces (D21) and profiles (D22) once per process, and each emitter calls only it | Each E issue then wires one call and one env line, and a service cannot turn on one signal and forget another | H.3, E.2 to E.6 |
| D24 | Tempo and Pyroscope storage | Both monolithic, single replica, filesystem storage on a local-path volume (D11), retention by time from decision 20 with the keys S14 confirms on the pinned versions, and a size cap enforced by `WdfObsVolumeFilling` where the pinned version has no size key | The epic's open question 2 ("Do Tempo and Pyroscope retention keys exist as assumed in the pinned versions?") is S14's to answer, and an alert is the fallback that needs no product code | S14, P.1, P.2, A.2 |

### 0.4 Proposals the lanes pin

After lane 0 merges, `contracts/instrumentation/README.md` and `metrics.yaml` are the source and this section is only a pointer.

| Item | Proposal |
| --- | --- |
| Contract | `contracts/instrumentation/` version 3, owner #13, with the README's owner line changed, the Log fields section rewritten to D15 with its mapping from the context attribute names, the `external` section of D3 in the schema, the loader and new vectors, and the platform catalogue below. The `docs/contracts.md` row and `docs/architecture/15-contracts.drawio` change in the same PR. Consumers: every emitter, #14, #15, #16, #17 |
| Preset contract | `contracts/preset/` version 2 (R.1, a `revisit` of #2): the optional `obs_stack` of D19 in the schema, held to the reservation rule by the loader, with vectors `valid/obs-stack.yaml` and `invalid/node-memory-below-reservation/obs-stack-small.yaml`. Consumers: every epic through `just up`, #16, #17 |
| Catalogue, built names | every name of admission D14, dispatch D15 and the four emitted workflow names, with their bounded labels as the code declares them |
| Catalogue, new names (proposals, final names pinned by 0.1) | placement: `wdf_placement_place_seconds`, `wdf_placement_decisions_total{pool,verdict}`, `wdf_placement_escalations_total{pool}`, `wdf_placement_snapshot_age_seconds`; workers: `wdf_worker_tasks_total{pool,exit_class}`, `wdf_worker_task_seconds{pool,exit_class}`, `wdf_worker_heartbeat_lag_seconds{pool}`, `wdf_worker_aggregate_inputs_total`, `wdf_worker_aggregate_rows_total`; workflow: `wdf_workflow_job_seconds{job_class,outcome}`, `wdf_workflow_jobs_total{job_class,outcome}`; admission: `wdf_admission_cap_jobs{scope}`; state inputs: `wdf_state_<input name>` with the input's unit; `wdf_obs`: `wdf_obs_log_dropped_total{component}`, `wdf_obs_span_dropped_total{component}` |
| Catalogue, external | the Temporal SDK slot, poll and schedule-to-start series, the Temporal server backlog, cache and heartbeat timeout series, RabbitMQ detailed queue series, kube-state-metrics pod, deployment, node and last-terminated series, cAdvisor container memory and CPU series, kubelet volume stats, SeaweedFS bucket series, and the self-metrics of Prometheus, Loki, Tempo, Pyroscope, Grafana and the collector, each named exactly after S14 |
| Label values | `pool` is `compile`, `simulate`, `aggregate`; `exit_class` is the classes of `contracts/workload/exit-classes.yaml`, held equal by a test; `job_class` and `priority_class` as declared today |
| Scrape jobs | `wdf_obs.targets.JOBS`, held equal to the rendered Prometheus configuration by a T0 test, read by the telemetry row |
| Rule files | `deploy/obs/prometheus/rules/<area>.yaml`, tests in `deploy/obs/prometheus/rules/tests/<area>.yaml` |
| Dashboards | `deploy/obs/grafana/dashboards/<name>.json`, one per dashboard |
| Stubs | `wdf_obs.testing.FakeObs`: one `ThreadingHTTPServer` answering `/api/v1/query`, `/api/v1/targets`, `/loki/api/v1/query_range`, Tempo's `/api/traces/<trace id>` and Pyroscope's label values call for `service_name`, from canned data; and `wdf_obs.query` (`prom(address, expr)`, `targets(address)`, `loki(address, query, start, end)`, `trace(address, trace_id)`, `profiled(address, start, end)`), the one client every lane and the gate use; its conformance test runs the same calls against the real stack in S.1, S.2, P.1 and P.2 |

## 1. Goal and non-goals

Goal (from the epic): observability for the job system itself, metrics, logs, traces, profiles and alerts viewed in Grafana, with no job UI.
Fleet health, queue depths and alerts live in Grafana.
The epic owns the contract that makes every component emit consistently, the stack that stores it, and the dashboards and alerts as code.

This pass delivers, under section 0: the observability switch on `ci` and `soak`; the instrumentation contract v3 with the renamed log fields and the catalogue; `wdf_obs`; the stack behind the switch (Prometheus, kube-state-metrics, Loki, one collector, Tempo, Pyroscope, Grafana, and a state inputs exporter); JSON logs, metrics, trace export and profiles in every Python service; the alert set with `promtool` tests; the dashboards; the catalogue contract test; and the telemetry row of `just smoke`.

Where the epic body is overtaken:

- "Two tiers ... mapped to #2's Tilt profiles" becomes one stack behind one switch (decisions 22 and 24); `deploy/dummy/obs/` and the `full` profile's place-holder obs tier go, and the comment of `deploy/tilt/profiles.yaml:3-4` that names two tiers is corrected in R.1.
- "shipped by the collector's `filelog` receiver" becomes an OTLP push to one collector Deployment (decision 23).
- "the per-job timeline is Temporal's own UI" becomes the Loki query by `job_id`, the control status route and the `temporal` CLI (decision 26).
- "a 7.9 GiB host" in Risks is stale: S2 moved `ci` to a CI runner and `soak` to a 20 GiB VM (`docs/delivery/spikes/README.md`, S2 row), and the switch-on stack only ever runs on the local host, whose `MemTotal` is 20,482,272 kB [read].

Deferred, and to which epic:

- A Temporal UI (decision 26): a `revisit` of #6 if wanted.
- The max-load severity mode, threshold tuning under load, the trace sampling ratio and profile overhead at max load, retention measured at soak length, and the export of soak evidence: #16.
- Run and disruption annotations: written by #14 and #15 through Grafana's annotation API, which this pass only enables.
- Alerts proven under real disruption: #15.
- Log shipping of third-party components (decision 23).
- The stack in a GitHub CI job: not planned (decision 22); a change needs a new captain decision.

### 1.1 Mapping from the epic's detail issues

| Epic detail issue | Delivered by |
| --- | --- |
| 1 Instrumentation contract | 0.1 |
| 2 `wdf_obs` helper | 0.2 (skeleton, query client, fake), H.1 (logs), H.2 (metrics, registry bridge), H.3 (traces, profiles, setup) |
| 3 Stack manifests | R.1 (switch), R.2 (node figures and port), S.1, S.2, S.3, P.1, P.2 |
| 4 Prometheus configuration | S.1, I.1 (state inputs) |
| 5 Loki and the collector | S.2 (a Deployment under decision 23) |
| 6 Grafana provisioning and dashboards | S.3 (provisioning), G.1, G.2 |
| 7 Alert rules and `promtool` tests | A.1, A.2 |
| 8 OOM and exit class visibility | E.6 (worker counters), S.1 (exit code series), S.2 (events), A.2, G.2 |
| 9 Pyroscope | H.3 (SDK), E.2 to E.6 (wiring), P.2 (server) |
| 10 Tempo and traces | H.3 (provider), E.2 to E.6 (wiring), S.2 (collector pipeline), P.1 (server), T.1 (the trace path checked end to end) |
| 11 Retention and storage budget | S13 (dry run), S14 (retention keys), S.1, S.2, P.1, P.2 (enforcement), exit row 11 |
| 12 Catalogue contract test | C.1 |
| 13 kind pod and node health | S14 (series on kind), S.1 |
| 14 Characterization series | 0.1 (names and buckets), E.2 to E.6 (emitters), G.1 (dashboard) |
| 15 Telemetry row of `just smoke` | T.1 |

## 2. Interfaces

### Consumes (each pinned to a version)

| From | What | Pinned at | Stub used until it exists |
| --- | --- | --- | --- |
| #2 | Module contract, group `obs`, `wdf-obs` quota and LimitRange, pool label and taint | module v1, topology v1 | none, merged |
| #2, `revisit` | R.1: the switch of D19 (preset contract v2, `opt_in` profiles, the record, every reader) | preset v1, `deploy/tilt/profiles.yaml` | none: R.1 merges before S13 |
| #2, `revisit` | R.2: the `obs_stack` figures from S13 and the Grafana host port row | preset v2 (numbers only), `deploy/host/ports.yaml` | none: R.2 merges before S.1's T2 line |
| #4 | Instrumentation contract (taken over), `wdf_events.metrics`, `wdf_temporal.trace` | instrumentation v2 | none, merged |
| #4, `revisit` | E.1: a listener hook on `wdf_events.metrics.registry` | child issue of #4 | none: E.1 merges before H.2 |
| #7 | Port 15692, the detailed families, the client counters, the three alert rules | broker v1, instrumentation v2 | none, merged |
| #8, `revisit` | E.2: `wdf_obs.setup`, registry export, cap gauge in the admission service | admission v1 | none |
| #24, `revisit` | E.3: `wdf_obs.setup` and registry export in the dispatch service | dispatchapi v1 | none |
| #6, `revisit` | E.4: `wdf_obs.setup`, `wdf_workflow_job_seconds` and `wdf_workflow_jobs_total`, and, if S14 finds no server series, a heartbeat timeout counter | temporal v3, workflow v1 | none |
| #9, `revisit` | E.5: metrics endpoint and `wdf_obs.setup` in placement, decision log kept as `msg` | placement v1 | none |
| #10, `revisit` | E.6: SDK metrics port, worker metrics, `wdf_obs.setup`, exit classes | worker v1, workload v1 (`exit-classes.yaml`) | none |
| #3, #12 | `contracts/state/inputs.yaml`, `wdf_stateops.inputs`, the `wdf_reader` group role | state v4 | none, merged |
| Temporal chart | Pod annotations `prometheus.io/scrape` and port 9090 on every server pod | chart 1.8.0 default values [read] | none |
| #17 pass 1 | `just smoke` (recipe v1), `wdf_acceptance.smoke`, row and count-line format | smoke recipe v1 | none: T.1 waits for the gate's G.2 merged |

### Produces (each pinned precisely)

| To | What | Where it lives (path) | Shape | Version |
| --- | --- | --- | --- | --- |
| every epic, the captain | The observability switch | `WDF_OBS` at `just up`, the record `cluster.obs` in the state directory, `obs_stack` in `deploy/presets/{ci,soak}.yaml`, `opt_in` in `deploy/tilt/profiles.yaml`, `deploy/tilt/obs.star` | D19, D20; `up.sh` exit 2 for `on` under `GITHUB_ACTIONS` or on `dev`, exit 4 for a cluster recorded with the other value | preset v2 |
| every emitter, #14, #15, #16, #17 | Instrumentation contract and catalogue | `contracts/instrumentation/` | 0.4 | instrumentation v3 |
| every Python service, #16, #17 | `wdf_obs` | `packages/obs/` | `setup(component)`, `logs.bind(**fields)`, `metrics.runtime(address, buckets)`, `metrics.export_registry(meter)`, `query.prom`, `query.targets`, `query.loki`, `query.trace`, `query.profiled`, `targets.JOBS`, `testing.FakeObs`, `python -m wdf_obs.inputs` | obs v1 |
| every epic, the captain | Obs modules | `deploy/obs/{prometheus,kube-state-metrics,loki,collector,tempo,pyroscope,grafana,state-inputs}/` | module v1, group `obs`, `profiles: [ci]`, enabled only with the switch on | module v1 |
| #14, #15, #16, #17 pass 2 | Prometheus API and `ALERTS` | `svc/prometheus.wdf-obs:9090` through a 127.0.0.1 forward | Prometheus HTTP API | pinned image |
| #15, #17 pass 2 | Loki API | `svc/loki.wdf-obs:3100` through a 127.0.0.1 forward | Loki HTTP API, the fields of D15 | pinned image |
| #16, #17 pass 2 | Tempo and Pyroscope APIs | `svc/tempo.wdf-obs:3200`, `svc/pyroscope.wdf-obs:4040` through 127.0.0.1 forwards | Tempo HTTP API, Pyroscope HTTP API | pinned images |
| the captain, #14, #15 | Grafana | `127.0.0.1:13000` | dashboards of G.1 and G.2, Explore over the four datasources, `POST /api/annotations` with the state-directory credential | pinned image |
| #15, #16 | Alert rules | `deploy/obs/prometheus/rules/` | 0.4 | instrumentation v3 |
| #14, #16, #18, #17 pass 2 | `just smoke` row 11 | `packages/acceptance/src/wdf_acceptance/smoke/telemetry.py` | D16, exit codes of recipe v1 unchanged | smoke recipe v1 |

## 3. Lanes

Package, module and file names are proposals.
Lane 0 owns every file it creates until the owning lane takes over the glob.

| Lane | Workers | Owns (exclusive) | Issues | Needs |
| --- | --- | --- | --- | --- |
| 0 Contract and skeleton | 1 | `contracts/instrumentation/**`, the instrumentation row of `docs/contracts.md`, `docs/architecture/15-contracts.drawio` and its svg, `docs/architecture/05-observability-data-flow.drawio` and its svg, `packages/obs/{pyproject.toml,README.md}`, `packages/obs/src/wdf_obs/{__init__,query,targets}.py`, `packages/obs/src/wdf_obs/testing/**`, `packages/obs/tests/{conftest,test_wheel,test_query,test_catalogue_names,test_log_names}.py`, the `wdf-obs` lines of the root `pyproject.toml`, `uv.lock` (rerun rule), the `prometheus` line of the dev shell in `flake.nix` (for `promtool`), `deploy/obs/README.md` | 0.1, 0.2 | nothing (barrier, merged alone) |
| R Cluster revisit | 1 | `contracts/preset/**`, the preset row of `docs/contracts.md`, `deploy/presets/{ci,soak}.yaml`, `deploy/tilt/profiles.yaml`, `deploy/tilt/obs.star`, the switch lines of `Tiltfile`, `scripts/up.sh`, `scripts/nuke.sh`, `scripts/tilt/run.sh`, `scripts/cluster-verify.sh`, `scripts/cluster/{render,namespaces,budget}.sh`, `scripts/cluster/test_*.py`, `scripts/tilt/test_tiltfile.py`, `scripts/tilt/fixtures/**`, the Grafana row of `deploy/host/ports.yaml`, the switch and port sections of `docs/cluster-host.md` and `docs/dev-setup.md` | R.1, R.2 | nothing for R.1; S13 for R.2 |
| S Stack | 1 | `deploy/obs/{prometheus,kube-state-metrics,loki,collector,grafana}/**` except `deploy/obs/prometheus/rules/**` and `deploy/obs/grafana/dashboards/**`, `deploy/dummy/obs/**` (removed), the five image keys of `deploy/versions.yaml`, `packages/obs/tests/stack/**` except `packages/obs/tests/stack/{tempo,pyroscope}/**` | S.1, S.2, S.3 | lane 0, R.2 |
| P Traces and profiles stack | 1 (same as S) | `deploy/obs/{tempo,pyroscope}/**`, their two image keys of `deploy/versions.yaml`, `packages/obs/tests/stack/{tempo,pyroscope}/**` | P.1, P.2 | S.2 |
| H Helper | 1 | `packages/obs/src/wdf_obs/{logs,metrics,traces,profiles,setup}.py`, `packages/obs/tests/{logs,metrics,traces,profiles}/**` | H.1, H.2, H.3 | lane 0; H.2 also E.1 |
| I Inputs exporter | 1 (same as H) | `packages/obs/src/wdf_obs/inputs/**`, `packages/obs/tests/inputs/**`, `deploy/obs/state-inputs/**`, the image key of the exporter if it differs from `python` | I.1 | H.2, H.3, R.2 |
| E Emitters (`revisit` children) | 2 | E.1 `packages/events/src/wdf_events/metrics.py` and a new `packages/events/tests/test_metrics_listener.py`; E.2 `packages/admission/src/wdf_admission/service/**`, `deploy/admission/Tiltfile.module`, `packages/admission/tests/service/test_obs.py`; E.3 `packages/dispatchapi/src/wdf_dispatchapi/service/**`, the log lines of `packages/dispatchapi/src/wdf_dispatchapi/publish.py`, `deploy/dispatchapi/Tiltfile.module`, `packages/dispatchapi/tests/service/test_obs.py`; E.4 `packages/workflow/src/wdf_workflow/service/**`, the metric lines of `packages/workflow/src/wdf_workflow/job.py`, `deploy/workflow/Tiltfile.module`, `packages/workflow/tests/workflow/test_obs.py`; E.5 `packages/placement/src/wdf_placement/service.py`, `deploy/placement/Tiltfile.module`, `packages/placement/tests/test_obs.py`; E.6 `packages/worker/src/wdf_worker/{__main__,service,activity}.py`, `deploy/workers/pool.star`, `packages/worker/tests/test_obs.py`; each its package's `pyproject.toml` (the `wdf-obs` line only) and `uv.lock` (rerun rule) | E.1 to E.6 | lane 0; E.2 to E.6 also H.1, H.2, H.3 and R.1 |
| G Dashboards | 1 | `deploy/obs/grafana/dashboards/**`, `packages/obs/tests/dashboards/**` | G.1, G.2 | lane 0 |
| A Alerts | 1 | `deploy/obs/prometheus/rules/**`, `packages/obs/tests/rules/**` | A.1, A.2 | lane 0, S14 |
| C Catalogue test | 1 (same as G) | `packages/obs/src/wdf_obs/catalogue.py`, `packages/obs/tests/catalogue/**` | C.1 | lane 0; its T2 line also S.1, P.1, P.2, E.2 to E.6, G, A |
| T Smoke row | 1 | `packages/acceptance/src/wdf_acceptance/smoke/telemetry.py`, the row-11 registration lines of the gate's driver, `packages/acceptance/tests/smoke/test_telemetry.py`, the `wdf-obs` line of `packages/acceptance/pyproject.toml`, `uv.lock` (rerun rule) | T.1 | lane 0, R.1, S.2, S.3, P.1, P.2, E.2 to E.6, the gate merged |

Shared-file rules:

- `uv.lock` is touched by 0.2, every E issue but E.1, I.1 and T.1; a conflict is never resolved by hand, and `uv lock` is rerun after rebase.
- `deploy/versions.yaml` is touched by S.1, S.2, S.3, P.1 and P.2, each only its own image keys, one per line.
- `docs/contracts.md` changes in 0.1 (its instrumentation row) and R.1 (its preset row), one row each.
- `flake.nix` changes only in 0.2, by one package line.
- `Tiltfile` changes only in R.1.
- Each E issue edits only its own package's files, its own module's `Tiltfile.module` (the `obs_env()` line) and one dependency line.

Departures from the method, stated so the review approves them knowingly:

- Lane 0 declares in `packages/obs/pyproject.toml` the full dependency set of the lanes (`temporalio`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, `pyroscope-io`, `pyyaml`, `wdf-events`, `wdf-stateops`), as #17's decision 20 did, because lanes H and I own modules inside a package whose `pyproject.toml` is lane 0's.
- T.1 edits files of #17's pass 1 under that spec's decision 11, which gives this row to this epic; #17 stays open for pass 2, so this is not a `revisit` of a closed epic.
- The E issues are `revisit` children of five closed epics because their specs hand the emitters to this epic (decisions 10 to 16), and R.1 and R.2 are `revisit` children of #2 because the switch changes #2's preset contract, profiles and scripts (decision 22).

## 4. Issues

One block per issue, in the lane issue template, replaced by `#NNN` links after filing.
Done means, for every issue: `just ci` green on the branch, the output of the named tests pasted in the PR, and nothing outside Owns and the shared files changed.

### 0.1 Instrumentation contract v3 and the catalogue

Lane 0, tier T0. Needs: nothing. Blocks: every other issue but R.1, S13 and S14.

- Scope: the contract-change PR that takes the contract over (owner #13), rewrites the Log fields section to D15 as a breaking change with the mapping from the context attribute names, adds the `external` section of D3 to the schema and loader, the platform catalogue of 0.4 with bounded labels and pinned buckets (D6), and leaves out the two names of D5; the `docs/contracts.md` row and diagram 15 in the same PR.
  The PR lists the emitters it migrates (E.2 to E.6) as its consumer issues, because no service emits a JSON line with the version 2 names today: every service logs plain text (`packages/workflow/src/wdf_workflow/service/__main__.py:16`, `packages/admission/src/wdf_admission/service/__main__.py:17`, `packages/dispatchapi/src/wdf_dispatchapi/service/__init__.py:320`, `packages/worker/src/wdf_worker/__main__.py:23`, `packages/placement/src/wdf_placement/service.py:225` [read]), and the one structured `extra` in the code uses `event_type` and `cloudevent_id` (`packages/events/src/wdf_events/codec.py:54`), which keep their names.
- Owns: `contracts/instrumentation/**`, the `docs/contracts.md` row, diagram 15.
- Consumes / produces: instrumentation v2. Produces instrumentation v3; consumer issues listed in the PR: E.1 to E.6, H.1, I.1, C.1, A.1, A.2, G.1, G.2, T.1, and the open epics #14, #15, #16, #17, #18.
- Acceptance criteria:
  - [ ] `scripts/verify/cluster/lib/vectors.sh contracts/instrumentation contracts/instrumentation/metrics.yaml` exits 0, with new vectors `valid/external.yaml`, `invalid/schema-violation/external-without-job.yaml` and `invalid/denied-label/external-jobid.yaml`.
  - [ ] `pytest packages/events -v -k instrumentation` passes unchanged.
  - [ ] `scripts/check-contracts-map.sh` exits 0, and `scripts/export-diagrams.sh --check` exits 0.
- Verifier: the vectors, `packages/events/tests/test_instrumentation.py`.

### 0.2 Package skeleton, query client, fake and diagram

Lane 0, tier T0. Needs: 0.1. Blocks: every lane after lane 0.

- Scope: `packages/obs` with the dependency set of section 3, README, wheel test; `wdf_obs.query` and `wdf_obs.testing.FakeObs` of 0.4; `wdf_obs.targets.JOBS`; a static test that no log call names a version 2 log field; the dev shell line for `promtool`; `deploy/obs/README.md`, which states the switch and links D19's owner docs; diagram 05 redrawn for this pass.
- Owns: lane 0's globs except 0.1's.
- Consumes / produces: instrumentation v3. Produces the package and the stub every lane builds on.
- Acceptance criteria:
  - [ ] `pytest packages/obs -v -k "wheel or query"` passes: the wheel imports `wdf_obs` and `pyroscope` outside the repository, and `prom`, `targets`, `loki`, `trace` and `profiled` read the canned answers of `FakeObs`.
  - [ ] `pytest packages/obs/tests/test_catalogue_names.py -v` passes: every name in the `COUNTERS`, `GAUGES` and `HISTOGRAMS` maps of the admission and dispatch metrics modules and every name created in `packages/workflow/src/wdf_workflow/job.py` is in the catalogue, and the `exit_class` values equal `contracts/workload/exit-classes.yaml`.
  - [ ] `pytest packages/obs/tests/test_log_names.py -v` passes: no `extra=` or `logs.bind` call under `packages/*/src` names `jobid`, `taskid`, `attempt` or `traceparent`, and a planted call naming `jobid` fails it.
  - [ ] `nix develop -c promtool --version` prints a version.
  - [ ] `scripts/export-diagrams.sh --check` exits 0, and diagram 05 shows pull scrapes, one collector for logs, events and traces, Tempo, Pyroscope, Grafana on 127.0.0.1, and the switch around the whole stack.
- Verifier: `packages/obs/tests/test_wheel.py`, `test_query.py`, `test_catalogue_names.py`, `test_log_names.py`.

### R.1 The observability switch (revisit of #2)

Lane R, tier T0. Needs: nothing. Blocks: S13, R.2, E.2 to E.6, T.1. Label: `revisit`, child of #2.

- Scope: D19 and D20: preset contract v2 with `obs_stack` (D9's estimates on `ci` and `soak`), `opt_in: [obs]` on the `ci` and `soak` profiles and the corrected header comment of `deploy/tilt/profiles.yaml`, the record `cluster.obs` in `up.sh` and its removal in `nuke.sh`, the switch in `render.sh`, `namespaces.sh`, `budget.sh`, `scripts/tilt/run.sh`, `cluster-verify.sh` and the `Tiltfile`, `deploy/tilt/obs.star`, the `GITHUB_ACTIONS` refusal, and a switch section in `docs/cluster-host.md` with the one-line local and smoke procedure in `docs/dev-setup.md`; the dated Closure addendum line of `02-cluster.md` gets this issue's link.
- Consumer epics affected, listed in the issue: #13, #16, #17 (a `ci` cluster brought up before R.1 is recorded as switch off; nothing needs `just nuke` until someone turns the switch on).
- Acceptance criteria:
  - [ ] `scripts/verify/cluster/lib/vectors.sh contracts/preset deploy/presets/*.yaml` exits 0 with the two new vectors of 0.4.
  - [ ] `scripts/cluster/budget.sh ci`, `WDF_OBS=on scripts/cluster/budget.sh ci`, `scripts/cluster/budget.sh soak` and `WDF_OBS=on scripts/cluster/budget.sh soak` each exit 0, and the switch-on runs print the `obs` node line at the `obs_stack` allocatable; `WDF_OBS=on scripts/cluster/budget.sh dev` exits 2.
  - [ ] `pytest scripts/cluster -q` passes, with new tests that render `ci` both ways (the obs node's container limit is 640 MiB off and the `obs_stack` figure on, and `namespaces.sh render` gives the matching `wdf-obs` quota), that `up.sh` with `GITHUB_ACTIONS=true WDF_OBS=on` exits 2 before it renders anything, that `up.sh` exits 4 when the record differs from `WDF_OBS` on an existing cluster, and that no file under `.github/workflows/` names `WDF_OBS`.
  - [ ] `pytest scripts/tilt -q` passes with a fixture module in group `obs`: it is enabled for `--profile ci` only when `run.sh` passes the switch on, and `cluster-verify.sh`'s module count for `ci` excludes it with the record at `off`.
- Verifier: the preset loader, `scripts/cluster/test_*.py`, `scripts/tilt/test_tiltfile.py`.

### S13 Stack footprint (spike)

Lane S, tier T2, timeboxed 2 days. Needs: R.1 (throwaway code only otherwise). Blocks: R.2, S.1, S.2, S.3, P.1, P.2, I.1.

- Question: does the whole stack of D9, Tempo and Pyroscope included, run on the `ci` obs node at its `obs_stack` estimate, how fast does its storage grow, and what do the trace and profile wiring cost the service and worker pods?
- Method: `WDF_OBS=on just up` on preset `ci`, `scripts/tilt/run.sh ci`, the stack applied from `docs/delivery/spikes/S13/code/` by `kubectl apply` at D9's limits, scraping every target of D1, with the services run once with throwaway OTLP and Pyroscope settings and once without; three runs of the smoke job, then the workflow harness with 2,000 jobs for at least one hour; peak working set and CPU throttling per container from cAdvisor, Prometheus head series, and the bytes on each volume at start and end.
- Pass line: no obs container restarts or is OOM killed, every obs container's peak working set is at most 80 percent of its D9 limit, every service and worker container stays at most 80 percent of its own limit with the wiring on, and `WDF_OBS=on scripts/cluster/budget.sh ci` with the proposed figures exits 0.
- Settles: R.2's figures (memory, and CPU from the throttled share), D9's limits, the retention caps of S.1, S.2, P.1 and P.2 from bytes per hour, and whether D22's sample rate needs lowering for the worker pods.
- Result: `docs/delivery/spikes/S13/stack-footprint.md`.

### S14 Series and keys on the pinned versions (spike)

Lane A, tier T2, timeboxed 1.5 days. Needs: nothing. Blocks: A.1, A.2, C.1's external list, E.4's heartbeat line, P.1, P.2.

- Question: which of the series the rules need exist on Temporal server 1.32.2, the SDK of `uv.lock`, kube-state-metrics at its newest release, and the kind node image of `deploy/versions.yaml`, and which retention keys Tempo and Pyroscope have at their newest releases?
- Pass line: all present, each with the named label: (1) a Temporal matching series of backlog count and backlog age with `task_queue`; (2) a Temporal series counting activity heartbeat timeouts with `task_queue`; (3) SDK `temporal_activity_schedule_to_start_latency` and the worker slot series from a pool worker with `task_queue`; (4) `kube_pod_container_status_last_terminated_exitcode` reading 137 after a throwaway pod with a 32 MiB limit runs `tail /dev/zero` on the obs node (not the node-wide memory canary); (5) a cAdvisor series that rises when a container hits its limit on cgroup v2 (the `memory.events` `max` signal of S10); (6) kubelet volume stats for a local-path claim; (7) a time retention key in Tempo and in Pyroscope, and whether either has a size cap.
- Fallbacks if absent, decided in the result: (2) the workflow counter of E.4, (5) the alert dropped and #6's leftover noted, (6) Prometheus and Loki self-reported storage bytes, (7) D24's alert-only size cap.
- Result: `docs/delivery/spikes/S14/series.md`.

### R.2 Obs node figures and Grafana port (revisit of #2)

Lane R, tier T0 and T2. Needs: S13. Blocks: S.1's T2 line. Label: `revisit`, child of #2.

- Scope: the `obs_stack` figures of `ci` and `soak` at S13's result (default 2,304 MiB, 1.5 CPU), and the Grafana row `grafana`, host port 13000, NodePort 30300, with the port table of `docs/cluster-host.md`.
- Consumer epics affected, listed in the issue: #13, #16, #17 (every `ci` cluster needs `just nuke` once for the port row).
- Acceptance criteria:
  - [ ] `contracts/preset/check.sh deploy/presets/ci.yaml`, `contracts/preset/check.sh deploy/presets/soak.yaml` and `contracts/preset/check.sh deploy/host/ports.yaml` exit 0; `scripts/cluster/budget.sh ci`, `WDF_OBS=on scripts/cluster/budget.sh ci` and `scripts/cluster/slots.sh ci` exit 0.
  - [ ] T2, switch on: `just nuke`, `WDF_OBS=on just up` exit 0 and `kubectl --context kind-wdf get node -l wdf.dev/pool=obs -o jsonpath='{.items[0].status.allocatable.memory}'` matches the reservation rule for the `obs_stack` size; `scripts/cluster-verify.sh` exits 0.
  - [ ] T2, switch off: `just nuke`, `just up` exit 0, the same query matches the 640 MiB node, and `scripts/cluster-verify.sh` exits 0.
- Verifier: the preset loader, `scripts/cluster/test_*.py`.

### H.1 JSON logs and OTLP push

Lane H, tier T0 and T1. Needs: 0.2. Blocks: H.3, E.2 to E.6, S.2's T2 line.

- Scope: `wdf_obs.logs` of D14 and D15: the stdout JSON handler, the OTLP log exporter when the endpoint is set, and `logs.bind(**fields)` over `contextvars`, which takes only the D15 names and derives `trace_id` and `span_id` from a bound `traceparent` argument.
- Acceptance criteria:
  - [ ] `pytest packages/obs/tests/logs -v` passes: each line is one JSON object with the D15 fields; `bind(traceparent=...)` yields `trace_id` and `span_id` and no `traceparent` field; `bind(jobid=...)` raises `TypeError`; with the endpoint unset no exporter is created; with the endpoint refusing connections, 10,000 lines log in under 2 s and `wdf_obs_log_dropped_total` counts the drops; with an in-test OTLP HTTP receiver, the received records carry `service.name` and the fields as attributes.
  - [ ] `pytest packages/obs/tests/logs -v -m integration` passes against the pinned collector image through `wdf_itest`, which forwards one record to a file exporter.
- Verifier: `packages/obs/tests/logs/`.

### H.2 Metrics helpers and the registry bridge

Lane H, tier T0. Needs: 0.2, E.1. Blocks: E.2 to E.6, I.1.

- Scope: `wdf_obs.metrics.runtime(address, buckets)` (the Temporal runtime with the catalogue's buckets), a meter wrapper that refuses a name or label not in the catalogue, and `export_registry(meter)` that mirrors `wdf_events.metrics.registry` through E.1's listener.
- Acceptance criteria:
  - [ ] `pytest packages/obs/tests/metrics -v` passes: an uncatalogued name or label raises; the runtime's endpoint serves a registry counter and `wdf_event_bytes` buckets after `to_bytes` runs; buckets equal the catalogue's.
- Verifier: `packages/obs/tests/metrics/`.

### H.3 Traces, profiles and the setup call

Lane H, tier T0 and T1. Needs: H.1. Blocks: E.2 to E.6, I.1, P.1's and P.2's T2 lines.

- Scope: `wdf_obs.traces` of D21, `wdf_obs.profiles` of D22, and `wdf_obs.setup(component)` of D23, which is idempotent within a process.
- Acceptance criteria:
  - [ ] `pytest packages/obs/tests/traces -v` passes: with the endpoint unset, `setup` installs no provider and `wdf_temporal.trace.current_traceparent()` inside `stage_span` returns the parent's `traceparent`, as today; with an in-test OTLP HTTP receiver, a `stage_span` parented on a known `traceparent` arrives with that trace id and `service.name`; with the endpoint refusing connections, 10,000 spans end in under 2 s and `wdf_obs_span_dropped_total` counts the drops.
  - [ ] `pytest packages/obs/tests/profiles -v` passes: with `WDF_PYROSCOPE_ADDRESS` unset `pyroscope.configure` is never called, and with it set it is called once with `wdf.<component>` and the `pool` tag.
  - [ ] `pytest packages/obs/tests/traces -v -m integration` passes against the pinned collector image, which forwards one span to a file exporter.
- Verifier: `packages/obs/tests/traces/`, `packages/obs/tests/profiles/`.

### I.1 State inputs exporter

Lane I, tier T1 and T2. Needs: H.2, H.3, S13, R.2. Blocks: C.1's T2 line.

- Scope: `python -m wdf_obs.inputs` and module `state-inputs` of D12, with its login created as the admission module creates its own, calling `wdf_obs.setup("state-inputs")` with `obs_env()`.
- Acceptance criteria:
  - [ ] `pytest packages/obs/tests/inputs -v -m integration` passes against Postgres through `wdf_itest` with the migrations applied: every `sql`, `sql_labelled` and `invariants` input is served as `wdf_state_<name>`, and a failing query keeps the last value and counts the error.
  - [ ] T2, switch on: after `scripts/tilt/run.sh ci`, `wdf_obs.query.prom` returns `wdf_state_connections` above 0.
- Verifier: `packages/obs/tests/inputs/`, the module's render test.

### E.1 Registry listener (revisit of #4)

Lane E, tier T0. Needs: 0.1. Blocks: H.2. Label: `revisit`, child of #4.

- Scope: `Registry.listeners`, called with each `inc` and `observe`, with no change to existing behaviour.
- Acceptance criteria:
  - [ ] `pytest packages/events -q -rs` passes with no `SKIPPED` line, and `test_metrics_listener` shows a listener receives each call once.

### E.2 Admission (revisit of #8)

Lane E, tier T0 and T2. Needs: H.1, H.2, H.3, R.1. Blocks: C.1's T2 line, T.1. Label: `revisit`, child of #8.

- Scope: `wdf_obs.setup("admission")` in place of `logging.basicConfig`, `job_id` and the claim's `traceparent` bound per claim, `export_registry`, `wdf_admission_cap_jobs{scope}`, and `obs_env()` in `deploy/admission/Tiltfile.module`.
- Acceptance criteria:
  - [ ] `pytest packages/admission -q -rs` passes with no `SKIPPED` line, and `test_obs` shows a JSON claim line with `job_id` and `trace_id` and the cap gauge per scope.
  - [ ] T2, switch on: `wdf_obs.query.prom(..., 'wdf_broker_settle_total')` returns a series from the admission pod after one `just smoke`.
  - [ ] T2, switch off: the admission pod's env has no `OTEL_EXPORTER_OTLP_ENDPOINT` and its log lines are JSON on `kubectl logs`.

### E.3 Dispatch (revisit of #24)

Lane E, tier T0 and T2. Needs: H.1, H.2, H.3, R.1. Blocks: as E.2. Label: `revisit`, child of #24.

- Scope: `wdf_obs.setup("dispatch")`, `job_id` and `trace_id` on each accepted job line and on the `publish.py` error lines that name a job today (`packages/dispatchapi/src/wdf_dispatchapi/publish.py:94`), `export_registry`, and `obs_env()` in `deploy/dispatchapi/Tiltfile.module`.
- Acceptance criteria: as E.2 with `pytest packages/dispatchapi -q -rs` and `wdf_broker_publish_total` from the dispatch pod.

### E.4 Workflow (revisit of #6)

Lane E, tier T0 and T1. Needs: H.1, H.2, H.3, R.1, S14. Blocks: as E.2. Label: `revisit`, child of #6.

- Scope: `wdf_obs.setup("workflow")` in the service and activities, `wdf_workflow_job_seconds` and `wdf_workflow_jobs_total` recorded through `workflow.metric_meter()` when a job ends, the heartbeat timeout counter only if S14 found no server series, and `obs_env()` in `deploy/workflow/Tiltfile.module`.
- Acceptance criteria:
  - [ ] `pytest packages/workflow -q -rs` passes with no `SKIPPED` line, the replay corpus included, and the PR removes or changes no `assert` line.
  - [ ] `test_obs` at T1 on the dev server: one job ends and both new series carry its `job_class` and `outcome`, and with an in-test OTLP receiver the activity spans of that job carry its trace id.

### E.5 Placement (revisit of #9)

Lane E, tier T0 and T2. Needs: H.1, H.2, H.3, R.1. Blocks: C.1's T2 line, T.1. Label: `revisit`, child of #9.

- Scope: the placement names of 0.4 on 9464 named `metrics`, `wdf_obs.setup("placement")` with the decision line kept as `msg`, and `obs_env()` in `deploy/placement/Tiltfile.module`.
- Acceptance criteria:
  - [ ] `pytest packages/placement -q -rs` passes with no `SKIPPED` line, and `test_obs` shows one decision counted by pool and verdict.
  - [ ] T2, switch on: `wdf_placement_decisions_total` is served after one `just smoke`.

### E.6 Workers (revisit of #10)

Lane E, tier T0 and T2. Needs: H.1, H.2, H.3, R.1. Blocks: C.1's T2 line, T.1. Label: `revisit`, child of #10.

- Scope: SDK metrics on 9464 named `metrics`, the worker names of 0.4 with `exit_class` from the runner (OOM counted for `oom_killed`, `compile_oom` and `aggregate_oom`), `wdf_obs.setup("<pool>-worker")` with JSON outcome logs carrying `job_id`, `task_id`, `attempt_id`, `pool` and `exit_class`, no line per heartbeat, and `obs_env()` in `deploy/workers/pool.star`.
- Acceptance criteria:
  - [ ] `pytest packages/worker -q -rs` passes with no `SKIPPED` line, and `test_obs` counts an exit class per pool and logs one outcome line per task.
  - [ ] T2, switch on: after one `just smoke`, `wdf_worker_tasks_total{pool="simulate",exit_class="ok"}` is at least 1.

### S.1 Prometheus and kube-state-metrics

Lane S, tier T0 and T2. Needs: lane 0, S13, R.2. Blocks: S.2, S.3, C.1, T.1.

- Scope: modules `prometheus` and `kube-state-metrics`, the discovery of D1, the rule files mounted from a ConfigMap, retention by time and size from S13, a volume per D11, and the removal of `deploy/dummy/obs/`, which nothing else references (`git grep dummy-obs` [read] finds only its manifest and the closed `02-cluster.md`).
- Acceptance criteria:
  - [ ] `pytest packages/obs/tests/stack -v -k prometheus` passes: the rendered configuration's jobs equal `wdf_obs.targets.JOBS`, and `promtool check config` passes on it.
  - [ ] `scripts/cluster/budget.sh ci` and `WDF_OBS=on scripts/cluster/budget.sh ci` exit 0, the switch-on run listing both modules.
  - [ ] T2, switch on: after `scripts/tilt/run.sh ci`, `wdf_obs.query.targets` shows every target up, `query.prom` returns `kube_node_status_allocatable` for six nodes, and `scripts/cluster-verify.sh` prints `ok ready` with the two modules counted.

### S.2 Loki and the collector

Lane S, tier T0 and T2. Needs: S.1, H.1. Blocks: S.3, P.1, T.1.

- Scope: modules `loki` (monolithic, filesystem, retention on from S13) and `collector` (one Deployment, OTLP receiver for logs and traces, Kubernetes event watch, Loki OTLP exporter, and a traces pipeline to `tempo.wdf-obs:4318` that drops while Tempo is absent).
- Acceptance criteria:
  - [ ] T2, switch on: a log line pushed from the host through a forward with `job_id=probe-<hex>` is returned by `query.loki` within 30 s, with `job_id` as structured metadata and not as a label (`/loki/api/v1/labels` has no `job_id`).
  - [ ] T2, switch on: `query.loki` returns the Kubernetes events of a pod started in `wdf-obs`.

### S.3 Grafana

Lane S, tier T0 and T2. Needs: S.2, R.2. Blocks: G.1's and G.2's T2 lines, the exit test.

- Scope: module `grafana` of D8 with four datasources (Prometheus, Loki, Tempo, Pyroscope), a Loki derived field that links `trace_id` to Tempo, and dashboard provisioning.
- Acceptance criteria:
  - [ ] T2, switch on: `curl -s http://127.0.0.1:13000/api/health` answers `"database": "ok"`, and `/api/datasources` lists the four datasources.
  - [ ] No credential is in a tracked file (`git grep` for the generated password finds nothing).

### P.1 Tempo

Lane P, tier T0 and T2. Needs: S.2, H.3, S14. Blocks: T.1, C.1's T2 line.

- Scope: module `tempo` (monolithic, OTLP receiver, filesystem storage, retention of D24), its scrape target, and the conformance of `query.trace`.
- Acceptance criteria:
  - [ ] `scripts/cluster/budget.sh ci` and `WDF_OBS=on scripts/cluster/budget.sh ci` exit 0.
  - [ ] T2, switch on: a span pushed from the host to the collector with a known trace id is returned by `query.trace` within 30 s, and Grafana's Tempo datasource health check answers ok.
  - [ ] T2, switch on: after one `just smoke`, `query.trace` on the job's `trace_id` returns spans of `dispatch`, `admission` and `workflow`.

### P.2 Pyroscope

Lane P, tier T0 and T2. Needs: S.2, H.3, S14. Blocks: T.1, C.1's T2 line.

- Scope: module `pyroscope` (monolithic, filesystem storage, retention of D24), its scrape target, and the conformance of `query.profiled`.
- Acceptance criteria:
  - [ ] `scripts/cluster/budget.sh ci` and `WDF_OBS=on scripts/cluster/budget.sh ci` exit 0.
  - [ ] T2, switch on: a host process that calls `wdf_obs.setup("probe")` with `WDF_PYROSCOPE_ADDRESS` on a forward is listed by `query.profiled` within 60 s, and Grafana's Pyroscope datasource health check answers ok.

### G.1 Dashboards: fleet, intake and admission, paused jobs, characterization

Lane G, tier T0 and T2. Needs: lane 0. Blocks: C.1.

- Scope: four dashboards; the characterization one shows every characterization series of 0.4 with its pinned buckets, for #16; per-job links of decision 26.
- Acceptance criteria:
  - [ ] `pytest packages/obs/tests/dashboards -v` passes: valid JSON, known datasource uids, no panel query names a label on the denylist.
  - [ ] T2, switch on: a screenshot of each dashboard after one `just smoke`, pasted in the PR, with no panel showing "No data" for a series the smoke run emits.

### G.2 Dashboards: placement and pools, workers, cluster health, stack health

Lane G, tier T0 and T2. Needs: lane 0. Blocks: C.1.

- Scope and acceptance as G.1 for these four, the workers dashboard showing exit classes and OOM counts per pool, and the stack health dashboard covering every obs module, Tempo and Pyroscope included, with each one's volume use and ingest rate.

### A.1 Alert rules: intake, admission, dispatch, workflow, events

Lane A, tier T0. Needs: lane 0, S14. Blocks: C.1.

- Scope: the three broker rules as pinned, `WdfIntakeWaitHigh`, `WdfReconcilerBacklogOld`, `WdfControlOutboxOld`, the admission rules of decision 11, `WdfJobPausedNeedsAttention`, `WdfResumeWaiting`, `WdfScheduleToStartBreached`, `WdfHistoryLengthHigh`, `WdfTemporalWorkerUnhealthy`, `WdfOldBuildIdLingering`, `WdfEventRejected`, each with a `promtool` test that fires and one that stays silent.
- Acceptance criteria:
  - [ ] `promtool check rules deploy/obs/prometheus/rules/*.yaml` and `promtool test rules deploy/obs/prometheus/rules/tests/*.yaml` print `SUCCESS`, run inside `just ci` by `packages/obs/tests/rules/test_promtool.py`.

### A.2 Alert rules: pools, workers, OOM, artifacts, cluster and the stack

Lane A, tier T0. Needs: lane 0, S14. Blocks: C.1.

- Scope: `WdfPoolBacklogAgeHigh`, `WdfUnschedulableTasks`, `WdfHeartbeatTimeoutStorm`, `WdfWorkerOOM`, `WdfContainerOOM`, `WdfPoolSaturated` (`info`), `WdfCompileFailuresHigh` and `WdfAggregateFailuresHigh` by class, `WdfCompileCacheMissStorm`, `WdfStoreCapacityHigh` (the inputs threshold of 70 percent), `WdfPodRestartUnexpected`, `WdfTargetDown`, `WdfWatchdog`, `WdfObsVolumeFilling` (Prometheus, Loki, Tempo and Pyroscope volumes, the size cap of D24), `WdfObsDataLoss` (the log and span drop counters of `wdf_obs` among its inputs), with `promtool` tests as A.1.
- Acceptance criteria: as A.1.

### C.1 Catalogue contract test

Lane C, tier T0 and T2. Needs: lane 0; T2 line also S.1, P.1, P.2, E.2 to E.6, I.1, G.1, G.2, A.1, A.2. Blocks: the exit test.

- Scope: `wdf_obs.catalogue`: every series name in a dashboard query or rule expression is in the catalogue (names or `external`), and, live, every catalogue entry appears in a scrape.
- Acceptance criteria:
  - [ ] `pytest packages/obs/tests/catalogue -v` passes, including a test that a dashboard naming `wdf_nope_total` fails it.
  - [ ] T2, switch on: `pytest packages/obs/tests/catalogue -v -m cluster` after one `just smoke` passes, and lists any entry absent from the scrape by name.

### T.1 Telemetry row of `just smoke`

Lane T, tier T0 and T2. Needs: R.1, S.2, S.3, P.1, P.2, E.2 to E.6, the gate's G.2 merged. Blocks: the exit test.

- Scope: row 11 of D16, which reads the switch from the record `up.sh` wrote.
- Acceptance criteria:
  - [ ] `pytest packages/acceptance/tests/smoke -v -k telemetry` passes over `FakeObs`: all targets up, six components' log lines, the trace's three services and the seven profiled components pass; a target down fails naming its job; a component with no line, no span or no profile after the deadline fails naming it and the signal; the record at `off` prints `skip telemetry: observability off`; rows 1 or 6 not ok print `skip`.
  - [ ] T2, switch on: `just smoke` three times on one deploy, each exit 0 with 11 rows `ok` and an elapsed under 300 s, outputs in the PR.
  - [ ] T2, switch off: `just smoke` once on a switch-off deploy, exit 0 with 10 rows `ok` and row 11 `skip`, output in the PR.

## 5. Order, stubs and spikes

Lane 0 (0.1, then 0.2) merges first, alone.
R.1 changes no file of lane 0 and runs alongside it; S14 changes no product file and runs as soon as the spec is approved; S13 runs when R.1 has merged, because it measures the stack on the switch-on `ci` node.
Then, in parallel: H.1, E.1, G.1, G.2 at once; A.1 and A.2 after S14; R.2 after S13; H.2 after E.1; H.3 after H.1; E.2 to E.6 after H.1, H.2, H.3 and R.1; I.1 after H.2, H.3 and R.2.
The T2 order is fixed by the single-owner cluster: S13, R.2 (both switch states), S.1, S.2, S.3, P.1, P.2, then the T2 lines of I.1, E.2 to E.6, G.1, G.2, then C.1's T2 line, then T.1, then the exit test.

The one stub is `wdf_obs.testing.FakeObs` with the client `wdf_obs.query`; its conformance test is the same `query` calls run against the real Prometheus and Loki in S.1 and S.2 and the real Tempo and Pyroscope in P.1 and P.2, so the fake cannot drift from what T.1 and C.1 read.

## 6. Test delta

- T0: the instrumentation vectors and loader, the preset vectors with `obs_stack`, the switch tests of R.1 (render, quota, budget both ways, the `GITHUB_ACTIONS` refusal, the record mismatch, no workflow naming `WDF_OBS`), the catalogue names test, the log names test, the wheel and query tests, the log format and non-blocking push, the meter wrapper and registry bridge, the tracer and profiler setup with the endpoints set and unset, the rendered Prometheus configuration, `promtool check` and `promtool test` of every rule file inside `just ci`, dashboard JSON checks, the static catalogue test, the telemetry row over `FakeObs` both ways, and each `revisit`'s package suite.
- T1: the OTLP log and span push through the pinned collector image, the inputs exporter against Postgres, the workflow's new series and spans on the dev server.
- T2: R.2's cluster both ways, each module's live check with the switch on, the live catalogue test, `just smoke` with row 11 both ways, and the exit test.
- Untested and why: alerts under real disruption (#15), thresholds, trace sampling and profile overhead under max load (#16), retention at soak length (#16), a Temporal UI (decision 26), third-party logs in Loki (decision 23), and the stack on a CI runner (decision 22: never deployed there).

## 7. Exit test

Run by a worker who did not write the lanes, on a clean export of `main`, under the lock, with `WDF_BUILD_ID=exit-a` on every `scripts/tilt/run.sh ci`.
Rows 1 to 13 run with the switch on, and rows 14 and 15 with it off.

| Row | Command | Expected | Evidence |
| --- | --- | --- | --- |
| 1 | `just ci` | exit 0, with the `promtool`, catalogue and switch tests in its output | output in the PR |
| 2 | `scripts/cluster/budget.sh ci`, `WDF_OBS=on scripts/cluster/budget.sh ci`, `scripts/cluster/slots.sh ci` | each exit 0, obs node lines `fits`; the switch-on run lists the eight obs modules and the switch-off run none | output in the PR |
| 3 | `grep -rn WDF_OBS .github/` and `GITHUB_ACTIONS=true WDF_OBS=on scripts/up.sh` | no match, and exit 2 with nothing rendered | output in the PR |
| 4 | `just nuke`, `WDF_OBS=on just up`, `WDF_BUILD_ID=exit-a scripts/tilt/run.sh ci`, `just cluster-verify` | Tilt `SUCCESS`, the eight obs modules ready, `cluster-verify` exit 0 | output in the PR |
| 5 | `just smoke` three times | each exit 0, 11 rows `ok`, elapsed under 300 s | the summaries in the PR |
| 6 | `pytest packages/obs/tests/catalogue -v -m cluster` | passes, no catalogue entry absent | output in the PR |
| 7 | `curl -s 'http://127.0.0.1:19090/api/v1/query?query=ALERTS{alertstate="firing"}'` over a forward of `svc/prometheus` | only `WdfWatchdog` | response body in the PR |
| 8 | apply the S14 OOM pod (32 MiB limit, `tail /dev/zero`) in `wdf-obs`, wait 2 minutes, query `ALERTS` and Loki for the pod's events, delete the pod | `WdfContainerOOM` firing, `kube_pod_container_status_last_terminated_exitcode` 137, the pod's events in Loki | responses in the PR |
| 9 | `kubectl --context kind-wdf -n wdf-control scale deployment/admission --replicas=0`, then `python -m wdf_broker.suite deadletter` with the `S` arguments of `10-broker.md:620` (the suite consumes the intake queue itself, so admission must not race it), then `--replicas=1`, wait 6 minutes and query `ALERTS`, then `python -m wdf_broker.dlq purge --yes` and wait 6 minutes again | `WdfDeadLetterQueueNotEmpty` firing, then resolved after the purge | responses in the PR |
| 10 | from one row 5 run: the job's `job_id` in Loki, its `trace_id` in Tempo, and the last 5 minutes of `wdf.workflow` in Pyroscope, through Grafana Explore | the log lines, a trace with spans of dispatch, admission and workflow reached from a log line's `trace_id` link, and a flame graph | screenshots in the PR |
| 11 | `query.prom` for `prometheus_tsdb_retention_limit_bytes`, and the configuration endpoints of Loki, Tempo and Pyroscope | the size and time caps of S13, Loki retention enabled, the Tempo and Pyroscope retention of D24 | responses in the PR |
| 12 | Grafana at `http://127.0.0.1:13000`, every dashboard after row 5 | a screenshot per dashboard, no panel in error | screenshots in the PR |
| 13 | `max_over_time(container_memory_working_set_bytes{namespace="wdf-obs"}[1h])` after rows 5 to 12 | every container under 80 percent of its limit | response in the PR, figures in Closure |
| 14 | `just nuke`, `just up`, `WDF_BUILD_ID=exit-a scripts/tilt/run.sh ci`, `just cluster-verify`, `kubectl --context kind-wdf -n wdf-obs get pods` | Tilt `SUCCESS`, `cluster-verify` exit 0 with no obs module counted, no pod in `wdf-obs`, the obs node at 640 MiB | output in the PR |
| 15 | `just smoke` once | exit 0, 10 rows `ok`, row 11 `skip telemetry: observability off` | the summary in the PR |

Not provable by this epic: alerts under real disruption and the noise of `WdfPodRestartUnexpected` during chaos (#15), thresholds and the severity mode under max load, the trace sampling ratio and profile overhead at 100,000 jobs per batch, and retention at soak length (#16), annotations written by a run (#14, #15), the Temporal UI (decision 26), third-party logs (decision 23), the stack on a CI runner (decision 22 keeps it off every GitHub CI job, and row 3 shows only that no workflow turns it on), and the two workflow names of D5.

## 8. Parallelism and cost

- Maximum parallel workers: 2 during lane 0 (lane 0 and R.1, with S14 on the cluster), 5 after it at T0 (H, two in E, G, A), 1 at T2 at any time, because the cluster is single-owner; at most two T1 test containers are shared across workers on the host, so the T1 lines of H.1, H.3, I.1 and E.4 queue on them.
- Host constraints: switch off, `ci` stays at 12,032 MiB and 8 CPUs; switch on, `ci` is 13,696 MiB and 9 CPUs at D9's estimate, within the local host's 20,002 MiB (`MemTotal` 20,482,272 kB [read]); `soak` switch on is 15,872 MiB.
  GitHub CI never runs the switch on, so the runner's 15,983 MiB no longer bounds the stack.
- CI cost: the `promtool` tests, the static catalogue test and the switch tests add seconds to `just ci`; no new workflow, and `kind-smoke` and `state-modules` run unchanged with the switch off.
- Estimates, not measurements, in worker-days: 0.1 (2), 0.2 (1.5), R.1 (2.5), S13 (2), S14 (1.5), R.2 (1), H.1 (1.5), H.2 (1), H.3 (1.5), I.1 (1.5), E.1 (0.5), E.2 (0.75), E.3 (0.75), E.4 (1.75), E.5 (1.25), E.6 (2.25), S.1 (2), S.2 (2), S.3 (1), P.1 (1.5), P.2 (1.5), G.1 (1.5), G.2 (1.75), A.1 (1.5), A.2 (1.5), C.1 (1.5), T.1 (1.5), exit test (2): about 42.5 worker-days, up from the draft's 30.5 by the switch (R.1), Tempo and Pyroscope (H.3, P.1, P.2, their wiring in E.2 to E.6), the renamed fields (0.1, 0.2), the larger spikes, and a second switch state in T.1 and the exit test.
- Elapsed, an estimate: about 19 to 22 working days with 4 to 5 workers, bounded by the T2 chain R.1, S13, R.2, S.1, S.2, S.3, P.1, P.2, T.1, exit test (about 17 days) plus the queue of the E lanes' T2 lines.

Risks:

- The footprint is still the first risk: S13's pass line is the gate, and a fail returns decision 22's node size to the captain with measured numbers; Pyroscope is the first component to drop if the whole stack does not fit, as the epic says.
- The trace and profile wiring adds memory to every service and worker pod with the switch on, and the simulate node keeps 22 MiB after its slot (`slots.sh ci` [read]); S13 measures the worker pods both ways, and D22's sample rate is the knob.
- A switch that disagrees with the cluster: the record of D19 and `up.sh`'s refusal guard it, but a hand-run `tilt up` with `--obs on` on a switch-off cluster would schedule the stack onto a 640 MiB node; `budget.sh` and the quota refuse the pods, so the failure is loud, not silent.
- Installing a real `TracerProvider` changes what `current_traceparent()` returns inside a span (a new span id per stage, as the contract intends) where it returned the parent's before; H.3's test pins both behaviours and E.4's replay corpus guards the workflow.
- `pyroscope-io` is a native wheel; 0.2's wheel test proves it installs for the pinned Python in the service images.
- Fresh `ci` clusters fail `tilt ci` liveness in about 4 of 20 cycles (https://github.com/AndoNorth/wasm-dag-forge/issues/528), and Prometheus adds watch load on the API server; D1 keeps kubelet scrapes off the API server.
- Temporal server metrics are high in series count; S.1 keeps only the series the catalogue's `external` section names, so head series stay bounded.
- The OTLP push drops logs and spans when the collector is down, by design (D14, D21); the drop counters show it.
- The Temporal SDK Prometheus exporter's naming (`counters_total_suffix`, `unit_suffix` off by default) is what makes catalogue names match; a runtime built elsewhere with those on would break the live catalogue test, which is the guard.
- Open intake issues can make row 5's jobs late, not wrong: https://github.com/AndoNorth/wasm-dag-forge/issues/520.

## 9. Closure (filled at close)

Not run yet.

Closure addenda, one dated line each, added after close when a `revisit` child issue is filed against this epic:

- none yet
