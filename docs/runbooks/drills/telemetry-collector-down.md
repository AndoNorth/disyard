# Drill: the telemetry collector is gone

Stop the collector that receives logs and traces from the services by OTLP.
The platform keeps working, and the logs and traces of every service stop reaching Loki and Tempo.

Runs on a cluster with the observability stack on, with the [alert watcher](README.md#watch-the-alerts) running.

## Break

```sh
kubectl --context kind-dsy -n dsy-obs scale deploy/collector --replicas=0
just smoke
uv run --frozen python -m dsy_dispatchapi.smoke --scenario submit --jobs 150
```

## Observe

| Signal | Where | Seen |
| --- | --- | --- |
| Smoke | row `telemetry` of `just smoke`, which exits 1 with 10 rows ok | `FAIL telemetry: after 60 s: no log line of dispatch; no log line of admission; no log line of workflow; no log line of simulate-worker; no log line of aggregate-worker; no span of dispatch, there is no trace id; ...` |
| Loki | `{service_name="dispatch"}` over the outage, with a range of minutes | 0 lines, while `kubectl logs deploy/dispatch` holds the 151 accepted lines of the same period |
| Query | `sum by (component) (dsy_obs_log_dropped_total)` and `dsy_obs_span_dropped_total` | `0` for every component, after the smoke run and after 150 more jobs |
| Alert | [DsyObsDataLoss](../alerts.md#dsyobsdataloss) | not fired |
| Alert | [DsyTargetDown](../alerts.md#dsytargetdown) | not fired, since a scaled-down pod has no scrape target |

The platform itself is unaffected: the smoke job ran and the other ten rows were ok.
The lost logs and spans are gone for good, and no alert and no drop counter says so.
The smoke row is the only check that notices, and it runs only when someone runs it.
The `stack-health` panel "Replicas available by module" shows the collector at `0`.

## Recover

```sh
kubectl --context kind-dsy -n dsy-obs scale deploy/collector --replicas=1
kubectl --context kind-dsy -n dsy-obs rollout status deploy/collector
just smoke
```

Real run, seconds counted from the scale-up: the collector was ready at +9 s, new log lines reached Loki at once (15 lines of `dispatch` within two minutes), and the next `just smoke` passed all 11 rows.
The logs and traces of the outage were not delivered afterwards.
