# Drill: the broker is gone

Stop the broker while a job is submitted.
The dispatch API still accepts the job, because accepting writes to the database and publishing to the broker is a separate, retried step.

Runs on a cluster with the observability stack on, with the [alert watcher](README.md#watch-the-alerts) running.

## Break

```sh
kubectl --context kind-dsy -n dsy-infra scale statefulset/rabbitmq --replicas=0
uv run --frozen python -m dsy_dispatchapi.smoke --scenario submit --jobs 1
```

The submit prints `accepted: 1` and `/readyz` of both API listeners still answers `200`, because readiness does not cover the broker.

## Observe

| Signal | Where | Seen |
| --- | --- | --- |
| Log | `kubectl --context kind-dsy -n dsy-control logs deploy/dispatch` | `the broker is unavailable, publishing waits: no connection to rabbitmq.dsy-infra.svc.cluster.local:5672` every 20 s |
| Log | the same on `deploy/admission` | `the broker probe failed: BrokerUnavailable(...)` |
| Query | `dsy_dispatch_unpublished_jobs` and `dsy_dispatch_unpublished_oldest_seconds` | `1`, and an age that grows by one second per second |
| Dashboard | `intake-admission`, panels "Accepted jobs not yet published" and "Oldest unpublished job age" | the count steps to 1 and the age climbs |
| Dashboard | `intake-admission`, panel "Breakers open" | the `broker` line reads 1 |
| Alert | [DsyAdmissionBreakerOpen](../alerts.md#dsyadmissionbreakeropen) with `dependency="broker"` | see the table below |
| Alert | [DsyReconcilerBacklogOld](../alerts.md#dsyreconcilerbacklogold) | see the table below |

Real run, seconds counted from the scale-down:

| At | What |
| --- | --- |
| +26 s | `DsyAdmissionBreakerOpen{dependency="broker"}` `pending` |
| +327 s | `DsyAdmissionBreakerOpen` `firing` |
| +663 s | `DsyReconcilerBacklogOld` `pending`, because the oldest unpublished job passed 600 s |
| +964 s | `DsyReconcilerBacklogOld` `firing` |

Two alerts that the runbooks lead an operator to expect did not fire, because a scaled-down pod has no scrape target and its series stop existing instead of dropping to zero:

- [DsyTargetDown](../alerts.md#dsytargetdown) never fired for the broker pod, since `up{pod="rabbitmq-0"}` had no series.
- [DsyIntakeQueueStalled](../alerts.md#dsyintakequeuestalled) never fired, since the queue series come from the broker's own endpoint.

## Recover

```sh
kubectl --context kind-dsy -n dsy-infra scale statefulset/rabbitmq --replicas=1
kubectl --context kind-dsy -n dsy-infra rollout status statefulset/rabbitmq
```

Real run, seconds counted from the scale-up:

| At | What |
| --- | --- |
| +13 s | `rabbitmq-0` ready |
| +59 s | the job `succeeded`, with no resend by the operator, and `dsy_dispatch_unpublished_jobs` back to 0 |
| +70 s | `DsyReconcilerBacklogOld` resolved |
| +90 s | `DsyAdmissionBreakerOpen` resolved |

For a short while after the pod returns, `DsyTargetDown` and `DsyIntakeQueueStalled` go `pending` and then clear on their own: the new pod's first scrape fails before it is ready, and the intake queue has no consumer until admission reconnects.
Neither fires inside its `for` window.
No dead letter appeared, and the queue contents survived the restart because the broker data is on a volume.
Run [`just smoke`](../validation-matrix.md#the-smoke-rows) to confirm.

## What the runbooks say

[DsyReconcilerBacklogOld](../alerts.md#dsyreconcilerbacklogold) says the reconciler publishes again by itself once the broker accepts, and this drill confirms it, with a restart of `dispatch` never needed.
