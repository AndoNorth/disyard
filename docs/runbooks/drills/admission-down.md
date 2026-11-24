# Drill: admission is gone

Stop admission while a job is submitted.
The job is accepted and published to its intake queue, and nothing consumes the queue, so the job waits at `accepted`.

Runs on a cluster with the observability stack on, with the [alert watcher](README.md#watch-the-alerts) running.

## Break

```sh
kubectl --context kind-wft -n wft-control scale deploy/admission --replicas=0
uv run --frozen python -m wft_dispatchapi.smoke --scenario submit --jobs 1
```

## Observe

| Signal | Where | Seen |
| --- | --- | --- |
| Queue | `kubectl --context kind-wft -n wft-infra exec rabbitmq-0 -- rabbitmqctl list_queues name messages consumers` | `intake.acme.standard 1 0`, one message and no consumer (the command also prints a `Timeout: 60.0 seconds` line first) |
| Dashboard | `intake-admission`, panels "Consumers by queue" and "Messages ready by queue" | consumers fall to 0 and the ready count steps to 1 |
| Job | `curl -s http://127.0.0.1:18080/v1/tenants/acme/jobs/<job id>` | `accepted`, not `admitted` |
| Alert | [WftIntakeQueueStalled](../alerts.md#wftintakequeuestalled) | see below |

Real run, seconds counted from the scale-down:

| At | What |
| --- | --- |
| +27 s | `WftIntakeQueueStalled{queue="intake.acme.standard"}` `pending` |
| +629 s | `WftIntakeQueueStalled` `firing`, as the rule waits 10 minutes with ready messages and no consumer |

Nothing else fired:
[WftTargetDown](../alerts.md#wfttargetdown) stays silent because a scaled-down pod has no scrape target and its series stop existing, and [WftAdmissionBreakerOpen](../alerts.md#wftadmissionbreakeropen) cannot fire because admission is the one that reports it.
The queue alert is therefore the only signal that admission is gone, and it needs the broker's own metrics to be present.

## Recover

```sh
kubectl --context kind-wft -n wft-control scale deploy/admission --replicas=1
kubectl --context kind-wft -n wft-control rollout status deploy/admission
```

Real run, seconds counted from the scale-up:

| At | What |
| --- | --- |
| +7 s | `admission` ready |
| +9 s | the queue shows `0 1`, no message and one consumer, and the `admission` log prints `claimed job <job id>, starting its workflow` |
| +46 s | `WftIntakeQueueStalled` resolved |

`WftTargetDown` goes `pending` for the new pod for about 30 s while its first scrape fails, and clears without firing.
No dead letter appeared and the job was admitted from the queue with nothing resent.
Run [`just smoke`](../validation-matrix.md#the-smoke-rows) to confirm.

## What the runbooks say

[WftIntakeQueueStalled](../alerts.md#wftintakequeuestalled) says to restart or repair admission, and this drill confirms that a restart is enough.
