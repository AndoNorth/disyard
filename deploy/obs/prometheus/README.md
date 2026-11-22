# Prometheus and kube-state-metrics

Metrics and alert rules of the observability stack: Prometheus and kube-state-metrics from the pinned images of `deploy/versions.yaml`, in `wft-obs` on the obs node.
Modules: `prometheus` here and `kube-state-metrics` in [its own directory](../kube-state-metrics/config.py).
Both are group `obs`, profile `ci`, and run only on a cluster brought up with `WFT_OBS=on`, as the [stack overview](../README.md) says.

## Files

| File | Holds |
| --- | --- |
| `module.yaml` | the module manifest: Prometheus at 100m of CPU and 640 MiB, requests equal to limits |
| `Tiltfile.module` | loads the objects that `config.py` renders and watches the rule files |
| `config.py` | renders the scrape configuration and every object of the module, and prints either with `scrape-config` and `render` |
| `rules/` | the alert rule files and their `promtool` tests, one file per area |

## What it scrapes

The jobs are `wft_obs.targets.JOBS`, held equal to the rendered configuration by `packages/obs/tests/stack`.

| Job | Targets |
| --- | --- |
| `kubernetes-pods` | every pod with a container port named `metrics` or `prometheus`, or the annotation `prometheus.io/scrape: "true"` with its `prometheus.io/port`, so no module names itself here |
| `rabbitmq-detailed` | port 15692 of the RabbitMQ pod, path `/metrics/detailed` with the families the queue alerts read |
| `kubelet`, `cadvisor` | each node's kubelet on port 10250 with the service account token of Prometheus, and not through the API server |

A new component joins the stack by naming its metrics port `metrics`.
The pod job keeps the labels a series names itself, so kube-state-metrics keeps its `namespace` and `pod`, and a series that has none gets those of its pod.
The interval is 30 seconds.

## Retention, volume and rules

Prometheus keeps 10 days and at most 4 GB, on a 5 GiB local-path volume that ends with `just nuke`, so the cap leaves room for the write-ahead log and the head.
The rule files of `rules/` and the configuration are mounted from ConfigMaps, and a checksum of each on the pod template rolls the pod when one changes, because Prometheus reads them only at start.
The strategy is `Recreate`: the volume takes one writer, and a second Prometheus beside the first would not fit the node.
The OTLP receiver is on, so the smoke gate pushes its results to `/api/v1/otlp/v1/metrics`.

## Reaching it

```sh
kubectl --context kind-wft -n wft-obs port-forward --address 127.0.0.1 svc/prometheus 19090:9090
```

`wft_obs.query` reads the API through that forward, and `ALERTS` shows the firing alerts.
