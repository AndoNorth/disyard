# Drill: a pool worker is gone

Break the simulate pool by scaling its worker to zero while a job needs it.
A job that reaches the simulate step then has nothing to run on.

Runs on a cluster with the observability stack on, after [one smoke run](../validation-matrix.md#the-smoke-rows) and with the [alert watcher](README.md#watch-the-alerts) running.

## Break

```sh
kubectl --context kind-dsy -n dsy-workers scale deploy/simulate-worker --replicas=0
uv run --frozen python -m dsy_dispatchapi.smoke --scenario submit --jobs 1
```

The submit prints `accepted: 1`, and the job is admitted, its compile is skipped on a cache hit, and its simulate task is placed on the `dsy-simulate` queue.

## Observe

| Signal | Where | Seen |
| --- | --- | --- |
| Pods | `kubectl --context kind-dsy -n dsy-workers get deploy` | `simulate-worker 0/0` |
| Dashboard | `placement-pools`, panel "Workers available by pool" | `simulate-worker` reads 0 at once |
| Job | `curl -s http://127.0.0.1:18080/v1/tenants/acme/jobs/<job id>` | `running` for as long as the worker is gone |
| Temporal | the job's workflow page, pending activity `run_task` | `PENDING_ACTIVITY_STATE_SCHEDULED`, attempt 1, no failure |
| Placement | `kubectl --context kind-dsy -n dsy-control exec deploy/placement -- python -m dsy_placement.show` | `simulate ... ready_workers=0` |
| Alert | the [alert watcher](README.md#watch-the-alerts) | none fired in 17 minutes, see below |

Real run, seconds counted from the scale-down:

| At | What |
| --- | --- |
| +13 s | `DsyTemporalWorkerUnhealthy` and, a second later, `DsyPoolSaturated` went `pending` for `dsy-simulate` |
| +73 s, +88 s | both left `pending` without ever firing, because the `dsy-simulate` series of `temporal_num_pollers` stopped existing (`absent(temporal_num_pollers{task_queue="dsy-simulate"})` was true) and a missing series is not a zero |
| +1031 s | still no alert, with the simulate task waiting for 17 minutes and `approximate_backlog_age_seconds{taskqueue="dsy_simulate"}` reading `0` throughout |

`DsyPoolBacklogAgeHigh` would have fired near +900 s (a backlog age over 600 s for 5 minutes) if the gauge had grown.
The queue backlog and the poller count are therefore blind to a pool whose worker is gone, so the only signals were the dashboard panel and the job that does not end.

## Recover

```sh
kubectl --context kind-dsy -n dsy-workers scale deploy/simulate-worker --replicas=1
kubectl --context kind-dsy -n dsy-workers rollout status deploy/simulate-worker
```

Real run: the pod was ready 9 s after the scale-up and the waiting job ended `succeeded` 45 s after it.
No dead letter and no manual step was needed, because the task stayed scheduled on its queue.
Run [`just smoke`](../validation-matrix.md#the-smoke-rows) to confirm the pool is healthy again.

## What the runbooks say

[DsyTemporalWorkerUnhealthy](../alerts.md#dsytemporalworkerunhealthy) and [DsyPoolBacklogAgeHigh](../alerts.md#dsypoolbacklogagehigh) describe this failure, and neither fired.
