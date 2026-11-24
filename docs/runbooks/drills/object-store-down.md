# Drill: the object store is gone

Stop the object store.
Every service that reads or writes an artifact loses it, and the dispatch API is the first to show it, because its readiness covers a read of the store.

Runs on a cluster with the observability stack on, with the [alert watcher](README.md#watch-the-alerts) running.
A full disk cannot be drilled on this cluster: the default storage class does not enforce the claim size, so `df` inside the pod reports the host file system, see [the sizing notes](../../../deploy/objectstore/README.md#sizing).
The full-disk recovery has its own steps in [the object store runbook](../../../deploy/objectstore/README.md#recovery-from-a-full-disk).

## Break

```sh
kubectl --context kind-wft -n wft-infra scale statefulset/objectstore --replicas=0
curl -s -w ' %{http_code}\n' http://127.0.0.1:18080/readyz
```

## Observe

| Signal | Where | Seen |
| --- | --- | --- |
| Readiness | `/readyz` on port 18080 | `{"status":"not-ready"} 503` within 8 s |
| Pod | `kubectl --context kind-wft -n wft-control get pods` | `dispatch 0/1`, so its Service has no endpoint |
| API | any call on ports 18080 and 18081, such as a submit or a status read | status `000`, a connection with no answer, and the smoke driver ends with `ConnectionResetError: [Errno 104] Connection reset by peer` |
| Log | `kubectl --context kind-wft -n wft-control logs deploy/dispatch` | `not ready: EndpointConnectionError: Could not connect to the endpoint URL: "http://objectstore.wft-infra.svc.cluster.local:8333/wft-sources/..."` |
| Alert | [WftTargetDown](../alerts.md#wfttargetdown) | `pending` at +18 s, gone at +78 s, never fired |
| Alert | [WftStoreCapacityHigh](../alerts.md#wftstorecapacityhigh), [WftAggregateFailuresHigh](../alerts.md#wftaggregatefailureshigh) | not fired, with no job running to fail |

The store outage takes the whole API with it, status reads and cancels included, because both listeners share the readiness of the one `dispatch` pod, so an operator cannot even ask about a job until the store is back.
`WftTargetDown` goes `pending` while the pod terminates and then clears, because a scaled-down pod has no scrape target left to be down.
No alert named the store as the cause.
The signals were the readiness status, the `dispatch` log and the pod list.

## Recover

```sh
kubectl --context kind-wft -n wft-infra scale statefulset/objectstore --replicas=1
kubectl --context kind-wft -n wft-infra rollout status statefulset/objectstore
curl -s -w ' %{http_code}\n' http://127.0.0.1:18081/readyz
```

Real run, seconds counted from the scale-up:

| At | What |
| --- | --- |
| +23 s | `objectstore-0` ready on all three containers |
| +28 s | `/readyz` answers `200` |

Data and credentials are on the volume claim, so nothing was lost, and [`just smoke`](../validation-matrix.md#the-smoke-rows) passed 10 rows on the first run and failed row `telemetry` with `no profile of aggregate-worker` after 60 s.
The second run passed all 11 rows, so the first failure was a late profile of the aggregate worker (the cause was not found) and not a store fault, because the row reads Pyroscope only.
