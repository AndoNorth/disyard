# Drill: the state exporter is gone

Stop `state-inputs`, the exporter that turns database and store inputs into the `dsy_state_*` series.
The alert rules that read those series lose their input.

Runs on a cluster with the observability stack on, with the [alert watcher](README.md#watch-the-alerts) running.

## Break

```sh
kubectl --context kind-dsy -n dsy-obs scale deploy/state-inputs --replicas=0
```

## Observe

| Signal | Where | Seen |
| --- | --- | --- |
| Query | `count({__name__=~"dsy_state_.+"})` | 18 before, and no result 75 s after |
| Query | `up{pod=~"state-inputs.*"}` | no series |
| Dashboard | `stack-health`, panel "Replicas available by module" | `state-inputs` reads `0`, drawn in the same green as the healthy modules |
| Alert | [DsyTargetDown](../alerts.md#dsytargetdown) | `pending` while the pod terminated and gone 60 s later, never fired in 6 minutes |

Nothing alerts on a missing exporter.
[DsyCompileCacheMissStorm](../alerts.md#dsycompilecachemissstorm) reads `dsy_state_cache_misses`, so it can no longer fire and says nothing about it.
The only signal is the dashboard panel, which has no red threshold for a zero.

## Recover

```sh
kubectl --context kind-dsy -n dsy-obs scale deploy/state-inputs --replicas=1
kubectl --context kind-dsy -n dsy-obs rollout status deploy/state-inputs
```

Real run, seconds counted from the scale-up: the pod was ready at +70 s, and the 18 `dsy_state_*` series were back at +80 s.
