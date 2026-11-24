# Drill: Temporal is gone while a job is cancelled

Stop the Temporal frontend while a job is running, and ask the control API to cancel the job.
The request is accepted and recorded, and it waits in the control outbox for a workflow that cannot be reached.

Runs on a cluster with the observability stack on, with the [alert watcher](README.md#watch-the-alerts) running.

## Set up

Hold one job at its simulate step, so it is still running when Temporal goes down:

```sh
kubectl --context kind-dsy -n dsy-workers scale deploy/simulate-worker --replicas=0
uv run --frozen python -m dsy_dispatchapi.smoke --scenario submit --jobs 1
kubectl --context kind-dsy -n dsy-control logs deploy/admission --since=1m | grep claimed
curl -s http://127.0.0.1:18080/v1/tenants/acme/jobs/<job id>
```

Go on when the status reads `running`.
The smoke driver opens its own forward to the Temporal frontend, so it cannot submit while the frontend is down, and this drill submits its job first.

## Break

```sh
kubectl --context kind-dsy -n dsy-infra scale deploy/temporal-frontend --replicas=0
curl -s -X POST http://127.0.0.1:18080/v1/tenants/acme/jobs/<job id>/cancel -d '{"reason": "drill: cancel while temporal is down"}'
```

## Observe

| Signal | Where | Seen |
| --- | --- | --- |
| Readiness | `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/readyz`, and the same on port 18081 | `503` on both within 5 s |
| Cancel | the response of the call above | `200` with `"outcome":"requested"` and `"view":"cancelling"`, so the request is taken while Temporal is down |
| Job | `curl -s http://127.0.0.1:18080/v1/tenants/acme/jobs/<job id>` | `"status":"running"`, `"view":"cancelling"`, `"control_delivered":false` |
| Log | `kubectl --context kind-dsy -n dsy-control logs deploy/dispatch`, and the workflow worker | `not ready: no answer in 6 s`, and `gRPC call ... retried N times ... Unavailable, message: "tcp connect error"` |
| Query | `dsy_control_outbox_oldest_seconds` | an age that grows by one second per second |
| Dashboard | `paused-jobs`, panels "Control requests not delivered" and "Oldest undelivered control request" | 1 request, and its age climbing |
| Alert | [DsyControlOutboxOld](../alerts.md#dsycontroloutboxold) and [DsyTemporalWorkerUnhealthy](../alerts.md#dsytemporalworkerunhealthy) | see below |

Real run, seconds counted from the scale-down:

| At | What |
| --- | --- |
| +41 s | `DsyTemporalWorkerUnhealthy` `pending` for eight Temporal system queues (`default_worker_tq` and seven `temporal_sys_*` queues) |
| +336 s | those eight `firing` |
| +361 s | `DsyControlOutboxOld` `pending`, because the request passed 300 s undelivered |
| +662 s | `DsyControlOutboxOld` `firing` |

`DsyTemporalWorkerUnhealthy` named only Temporal's own queues, and none of the `dsy-` queues of the platform, so the alert reads as a Temporal outage here and not as a lost worker.
`DsyAdmissionBreakerOpen{dependency="temporal"}` was not exercised, because no job reached admission during the outage.

## Recover

```sh
kubectl --context kind-dsy -n dsy-infra scale deploy/temporal-frontend --replicas=1
kubectl --context kind-dsy -n dsy-workers scale deploy/simulate-worker --replicas=1
```

Real run, seconds counted from the scale-up:

| At | What |
| --- | --- |
| +2 s | the frontend ready, and `/readyz` answers `200` on both ports |
| +42 s | the retrier delivered the request: the job reads `cancelled` with `control_delivered` true |
| +48 s | `DsyTemporalWorkerUnhealthy` resolved for the system queues |
| +73 s | `DsyControlOutboxOld` resolved |

Nothing was resent, because the retrier of `dispatch` delivers the request by itself, as [DsyControlOutboxOld](../alerts.md#dsycontroloutboxold) says.
For a moment `DsyTemporalWorkerUnhealthy` and `DsyPoolSaturated` went `pending` for `dsy-simulate` while its worker reconnected, and cleared inside their windows.
Run [`just smoke`](../validation-matrix.md#the-smoke-rows) to confirm.
