# Loki and the collector

Logs and Kubernetes events of the observability stack: Loki and the OpenTelemetry Collector from the pinned images of `deploy/versions.yaml`, in `dsy-obs` on the obs node.
Modules: `loki` here and `collector` in [its own directory](../collector/config.py).
Both are group `obs`, profile `ci`, and run only on a cluster brought up with `DSY_OBS=on`, as the [stack overview](../README.md) says.

## Files

| File | Holds |
| --- | --- |
| `module.yaml` | the module manifest: Loki at 100m of CPU and 256 MiB, requests equal to limits |
| `Tiltfile.module` | loads the objects that `config.py` renders |
| `config.py` | renders the Loki configuration and every object of the module, and prints either with `loki-config` and `render` |

The collector directory holds the same three files, at 50m of CPU and 160 MiB.

## Where the logs come from

| Source | Path |
| --- | --- |
| A Python service | pushes OTLP HTTP to `collector.dsy-obs:4318`, as `OTEL_EXPORTER_OTLP_ENDPOINT` says when the switch is on, and the collector forwards to Loki's OTLP endpoint `loki.dsy-obs:3100/otlp` |
| Kubernetes events | the collector watches `events.k8s.io` events of every namespace itself, and tags them `service_name=kubernetes-events` |

Temporal, RabbitMQ, Postgres and the store keep `kubectl logs` only.
The collector's traces pipeline forwards spans to `tempo.dsy-obs:4318` with no retry and no queue, so spans are dropped while Tempo is absent and the collector's memory does not grow.

## Labels and structured metadata

Loki indexes only its default OTLP resource attributes: `service_name`, `service_instance_id` and `k8s_namespace_name`.
The collector adds no processing that makes more, so `job_id`, `trace_id` and the other correlation fields of the log contract are structured metadata.
Filter on them after the selector, such as `{service_name="dispatch"} | job_id="<job id>"`, and never in the selector.
`/loki/api/v1/labels` lists the index labels, and `job_id` is not among them.

## Retention, volume and memory

Loki keeps 10 days, enforced by the compactor, on a 2 GiB local-path volume that ends with `just nuke`.
Loki has no size key, so only the volume alarm brakes a log flood.
The strategy is `Recreate`: the volume takes one writer, and a second Loki beside the first would not fit the node.
S13 measured a wide LogQL range killing Loki at 256 MiB, so a query over hours wants a narrow selector, a short range or both.

## Reaching it

```sh
kubectl --context kind-dsy -n dsy-obs port-forward --address 127.0.0.1 svc/loki 13100:3100
```

`dsy_obs.query.loki` reads the API through that forward.
