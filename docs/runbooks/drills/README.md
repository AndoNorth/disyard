# Failure drills

One drill per failure mode: break one component on purpose, see what the stack shows, and recover.
Each drill was run on a live cluster, and its page records the real output and how long each alert took, so the [alert response](../alerts.md) and [triage](../triage.md) pages are proven against a real failure and not only read.

## The drills

Times are seconds from the break to the first `firing` alert, from one run on the author's WSL2 host.
`none` means no alert fired, and the page says what the operator saw instead.

| Drill | Break | Alert that fired | After |
| --- | --- | --- | --- |
| [Pool worker down](pool-worker-down.md) | `simulate-worker` scaled to 0 with a job waiting | none in 17 minutes | none |
| [Broker down](broker-down.md) | `rabbitmq` scaled to 0 with a job submitted | `DsyAdmissionBreakerOpen`, then `DsyReconcilerBacklogOld` | 327 s, 964 s |
| [Admission down](admission-down.md) | `admission` scaled to 0 with a job submitted | `DsyIntakeQueueStalled` | 629 s |
| [Dead letter](dead-letter.md) | a truncated event published to intake | `DsyAdmissionDeadLettered`, then `DsyDeadLetterQueueNotEmpty` | 45 s, 336 s |
| [Temporal down](temporal-down.md) | `temporal-frontend` scaled to 0 with a cancel requested | `DsyTemporalWorkerUnhealthy` (Temporal's own queues), then `DsyControlOutboxOld` | 336 s, 662 s |
| [Object store down](object-store-down.md) | `objectstore` scaled to 0 | none | none |
| [State exporter down](state-exporter-down.md) | `state-inputs` scaled to 0 | none | none |
| [Telemetry collector down](telemetry-collector-down.md) | `collector` scaled to 0 | none | none |
| [Prometheus down](prometheus-down.md) | `prometheus` scaled to 0 | none, nothing evaluates alerts | none |
| [Container restart](container-restart.md) | the admission container stopped from the node | `DsyPodRestartUnexpected` | 32 s |

A drill that shows `none` is a finding about the alerts and not a failure of the drill, and its page names what the operator saw.

## Rules

- Run a drill only on a cluster that is yours: one cluster exists at a time, and every command here takes the cluster lock of [the operator runbook](../operator.md#the-cluster-rules).
- Run one drill at a time and recover before the next one, so an alert is never explained by two breaks.
  After recovering, run `just smoke` and confirm that only `DsyWatchdog` is left in the alerts.
- A drill needs the observability stack, so bring the cluster up with `DSY_OBS=on`, see [observability on](../operator.md#observability-on), and run `just smoke` once first so every series exists.
- The breaks scale a deployment to zero because that is the failure a rollout mistake or an eviction produces, and they are undone by scaling back to one.
  No drill deletes data.

## Watch the alerts

Alerts are read from Prometheus, so forward it, see [reading the firing alerts](../alerts.md#reading-the-firing-alerts), then in a second terminal print every change with the time:

```sh
prev=
while sleep 5; do
  cur=$(curl -s http://127.0.0.1:19090/api/v1/alerts | jq -r '.data.alerts[] | select(.labels.alertname != "DsyWatchdog") | "\(.labels.alertname) \(.state) \(.labels | del(.alertname, .severity, .area) | tojson)"' | sort)
  [ "$cur" != "$prev" ] && { date +%s; diff <(echo "$prev") <(echo "$cur"); prev=$cur; }
done
```

An alert goes `pending` when its expression is true, and `firing` after its `for` window.
The time to `firing` in a drill is therefore the window plus the delay of the scrape and the rule evaluation, which is up to a minute, because the scrape and the rule evaluation both run every 30 s.

## Dashboards and wiring

The check of every dashboard and of each service's metrics, logs and traces is [the live check](live-check.md).
