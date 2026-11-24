# Live check of dashboards and wiring

Open every dashboard against a running cluster and confirm that each service's metrics, logs and traces reach the stack.
Run it after a deploy that touched a dashboard or a service's telemetry, on a cluster brought up with the observability stack on.

## Prepare

```sh
DSY_OBS=on just up
scripts/tilt/run.sh ci
just smoke
just smoke
just smoke
```

Three smoke runs give the `smoke` dashboard three points per row and three annotations, and give every counter a second increment, which some rate panels need, see [the first job](#the-first-job-of-a-cluster).
Forward Prometheus as in [reading the firing alerts](../alerts.md#reading-the-firing-alerts).

## Dashboards

Each dashboard is opened with the browser of the [`ui-check` skill](../../../.claude/skills/ui-check/SKILL.md), at a viewport tall enough for the whole page:

```sh
chrome-devtools-axi emulate --viewport 1600x2600x1
scripts/ui-check.sh "http://127.0.0.1:13000/d/<uid>?kiosk&from=now-30m&to=now" <png>
```

The uids are the file names under [deploy/obs/grafana/dashboards](../../../deploy/obs/grafana/dashboards/README.md).
For a panel that shows `No data`, print the expressions of a dashboard and run the panel's expression in Prometheus:

```sh
jq -r '.. | objects | select(.title? and .targets?) | [.title, (.targets[].expr // empty)] | @tsv' deploy/obs/grafana/dashboards/<uid>.json
```

Result after three smoke runs: all nine dashboards load with no query error, and the panels with no data are these.

| Dashboard | Panel | Why it is empty |
| --- | --- | --- |
| `fleet`, `intake-admission` | "Dead letters by reason /s" | no dead letter happened, and the series appears with the first one |
| `fleet` | "Workflow retries by budget /s" | no retry happened |
| `placement-pools` | "Retries and reschedules" | no retry or reschedule happened |
| `characterization` | "dsy_event_size_warnings_total /s" | no event passed its size warning |
| `smoke` | "Failures per row over runs" | every row was ok, and a `FAIL` verdict is pushed only when a row fails |
| `workers` | "Worker containers last terminated by OOMKilled" | no worker was OOM killed |
| `cluster-health` | "Last termination reasons" | no container had terminated yet, and a restart fills it, see [the container restart drill](container-restart.md) |
| `cluster-health` | "Volume use by claim" | the kubelet of this cluster exports no `kubelet_volume_stats_*` series, so it never has data |
| `stack-health` | "Volume use by module", "Volume bytes used by module" | the same missing kubelet series |

The `smoke` dashboard after three runs shows `Runs in range` 3, `ok` for the last verdict, and one point per row per run with three annotations (`smoke ok, batch smoke-...`).

### The first job of a cluster

After the first smoke run the panels "Jobs ended in range" and "Jobs ended by outcome /s" of `fleet` read `No data`, although one job ended.
`dsy_workflow_jobs_total` is created with its first increment, so the first job leaves a single sample and `increase` and `rate` over one sample are empty.
They fill from the second job on.

## Wiring

For each service, one check per signal.
The service names are `dispatch`, `admission`, `workflow`, `placement`, `compile-worker`, `simulate-worker` and `aggregate-worker`.

Metrics, from the forward of Prometheus: every scrape job is up, and a series of the service exists.

```sh
curl -s http://127.0.0.1:19090/api/v1/targets | jq -r '.data.activeTargets[] | [.labels.job, .labels.namespace, .labels.pod, .health] | @tsv' | sort
curl -s http://127.0.0.1:19090/api/v1/query --data-urlencode 'query=dsy_broker_settle_total{pod=~"admission.*"}' | jq -c '.data.result[] | [.metric.call, .value[1]]'
```

Logs, from Loki through Grafana, with one service and a range of minutes:

```sh
now=$(date +%s)
curl -s -G http://127.0.0.1:13000/api/datasources/proxy/uid/loki/loki/api/v1/query_range \
  --data-urlencode 'query={service_name="admission"}' \
  --data-urlencode "start=$((now-1800))000000000" --data-urlencode "end=${now}000000000" --data-urlencode limit=5 \
  | jq -r '.data.result[].values[][1]'
```

Traces, from Tempo through Grafana, by service name:

```sh
curl -s -G http://127.0.0.1:13000/api/datasources/proxy/uid/tempo/api/search \
  --data-urlencode 'tags=service.name=dispatch' --data-urlencode "start=$((now-1800))" --data-urlencode "end=$now" --data-urlencode limit=5 \
  | jq '.traces | length'
```

Real run, after three smoke runs:

| Service | Metric checked | Loki lines in 30 minutes | Traces in 30 minutes |
| --- | --- | --- | --- |
| `dispatch` | `dsy_broker_publish_total{outcome="accepted"}` is 3 | 169 | 20 |
| `admission` | `dsy_broker_settle_total{call="ack"}` is 3, `dsy_admission_cap_jobs` is 1000 global and 2000 open | 4 | 1 |
| `workflow` | `dsy_workflow_jobs_total{outcome="succeeded"}` is 3 | 2, among them `job ended: ... outcome=succeeded` and `task skipped: compile-cache-hit` | 20 |
| `placement` | `dsy_placement_decisions_total{verdict="placed"}` is 3 for simulate and aggregate, and 1 for compile | 5, `placed type=...` | 0, placement starts no span |
| `compile-worker` | `dsy_worker_tasks_total{pool="compile",exit_class="ok"}` is 1 | 2 | 1 |
| `simulate-worker` | `dsy_worker_tasks_total{pool="simulate",exit_class="ok"}` is 3 | 2 | 1 |
| `aggregate-worker` | `dsy_worker_tasks_total{pool="aggregate",exit_class="ok"}` is 3 | 2 | 1 |

The compile worker ran once because the second and third smoke jobs hit the compile cache, which the `workflow` log line `task skipped: compile-cache-hit` records.
The three worker targets are `up`, and `kubectl --context kind-dsy -n dsy-workers exec deploy/<pool>-worker -- python -m dsy_worker.probe` exits 0 for each pool.
One job's trace holds spans of `dispatch`, `admission` and `workflow` (5 services, 37 spans in the run), found from the `trace_id` of a `dispatch` log line in Explore on the Tempo datasource.
`state-inputs` is up and exports 18 `dsy_state_*` series, and writes no log line at all in 30 minutes.

## With the switch off

On a cluster brought up without `DSY_OBS=on`, the same check shows the services without the stack:

- `just smoke` passes 11 rows, and row `telemetry` prints `ok telemetry: observability off, not checked`.
- No Deployment of `dispatch`, `admission`, `workflow`, `placement` or a pool worker has an `OTEL_EXPORTER_OTLP_ENDPOINT` variable, and `dsy-obs` holds no pod.
- Every service still logs one JSON object per line on `kubectl logs`.
- `kubectl --context kind-dsy -n dsy-workers exec deploy/<pool>-worker -- python -m dsy_worker.probe` exits 0 for each pool, and prints `collector: refused` and `pyroscope: refused`, which is the expected answer with the switch off.
