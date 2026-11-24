# Drill: Prometheus is gone

Stop Prometheus.
It holds the alert rules, so while it is down nothing evaluates an alert, and no alert fires for anything that breaks meanwhile.

Runs on a cluster with the observability stack on.
The alert watcher of [the drills page](README.md#watch-the-alerts) reads Prometheus itself, so it goes blind too, which is the point of the drill.

## Break

```sh
kubectl --context kind-wft -n wft-obs scale deploy/prometheus --replicas=0
```

## Observe

| Signal | Where | Seen |
| --- | --- | --- |
| Forward | `kubectl --context kind-wft -n wft-obs port-forward --address 127.0.0.1 svc/prometheus 19090:9090` | the forward ends, and `curl` of `http://127.0.0.1:19090/api/v1/alerts` answers status `000` |
| Dashboards | any Grafana dashboard, such as `fleet` | every panel that reads Prometheus shows `No data` with a red error marker, and the Grafana proxy of the `prometheus` datasource returns nothing |
| Smoke | row `telemetry` of `just smoke`, which exits 1 with 10 rows ok | `FAIL telemetry: kubectl --context=kind-wft --namespace=wft-obs port-forward --address=127.0.0.1 svc/prometheus did not listen in 15 s`, and the closing line `smoke results: the push failed, gauges: ...` |
| Alert | [WftWatchdog](../alerts.md#wftwatchdog) | absent, because the list that holds it is unreachable |

Nothing pages for this failure, since the rule that would is evaluated by the thing that is down.
The entry for [WftWatchdog](../alerts.md#wftwatchdog) says its absence means the rules are not evaluated, and the dashboards going empty is the first thing an operator sees.
A smoke run in this state still passes the ten rows that do not read Prometheus, and the job itself runs.

## Recover

```sh
kubectl --context kind-wft -n wft-obs scale deploy/prometheus --replicas=1
kubectl --context kind-wft -n wft-obs rollout status deploy/prometheus
kubectl --context kind-wft -n wft-obs port-forward --address 127.0.0.1 svc/prometheus 19090:9090
```

Real run, seconds counted from the scale-up:

| At | What |
| --- | --- |
| +24 s | the pod ready |
| +51 s | `WftWatchdog` listed as `firing` again, through a new forward |

The time series survived, because Prometheus keeps them on its volume: a query for a time before the outage, one hour back, returned its value (`6`) after the restart.
Samples from the outage itself are missing, because nothing scraped while it was down.
