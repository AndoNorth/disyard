# Dashboards

Each `<name>.json` is one Grafana dashboard, provisioned as it is, with `uid` equal to `<name>`.
Every series a panel reads is in the [instrumentation catalogue](../../../../contracts/instrumentation/metrics.yaml), and no panel groups or filters by a label on its denylist.

| Dashboard | Shows |
| --- | --- |
| `fleet` | Jobs live, open, paused and ended, the pools' task attempts and placement decisions, and the breakers, dead letters and retries |
| `intake-admission` | Dispatch's submits and outbox, the intake queues and broker client, and admission's starts, caps, waits and faults |
| `paused-jobs` | Paused jobs, the resumes waiting for a slot, pause and resume requests, and the control outbox |
| `characterization` | Every catalogued histogram as quantiles and as counts on its pinned buckets, the throughput counters and the saturation gauges |
| `smoke` | The last `just smoke` verdict, each row's seconds and failures over runs, and the run annotations tagged `smoke` |
| `placement-pools` | Placement decisions per pool, their latency and the snapshot they read, and each pool's workers |
| `workers` | Task attempts per pool by exit class, OOM counts per pool, durations and heartbeats |
| `cluster-health` | Nodes, pods and Deployments of the kind cluster, from kube-state-metrics, cAdvisor and the kubelet |
| `stack-health` | Every module of the observability stack: availability, memory, volume use and ingest rate |

## Datasources

A panel names its datasource by uid, and Grafana's provisioning declares these uids.

| uid | Type |
| --- | --- |
| `prometheus` | `prometheus` |
| `loki` | `loki` |
| `tempo` | `tempo` |
| `pyroscope` | `grafana-pyroscope-datasource` |
| `grafana` | `grafana`, Grafana's own store, which annotations read |

## Smoke results

The smoke gate pushes each row's gauges once per run, so a panel over runs reads `last_over_time(...[$__interval])`.
Each pushed sample then lands in exactly one step, which draws one point per row per run.

## Tests

`packages/obs/tests/dashboards/` checks every file: valid JSON, unique uids, provisioned datasource uids and types, and no denied label in a query or legend.
It also checks every histogram on the characterization dashboard, and the smoke dashboard's series and annotations.
