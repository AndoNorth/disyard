# Tempo

Traces of the observability stack: Tempo from the pinned image of `deploy/versions.yaml`, in `wft-obs` on the obs node.
It runs only on a cluster brought up with `WFT_OBS=on`, as the [stack overview](../README.md) says.

## Files

| File | Holds |
| --- | --- |
| `module.yaml` | the module manifest: Tempo at 200m of CPU and 1,024 MiB, requests equal to limits |
| `Tiltfile.module` | loads the objects that `config.py` renders |
| `config.py` | renders the Tempo configuration and every object of the module, and prints either with `tempo-config` and `render` |

## Where the spans come from

A Python service pushes OTLP HTTP to `collector.wft-obs:4318`, and the collector's traces pipeline forwards to Tempo's OTLP HTTP receiver at `tempo.wft-obs:4318`.
The collector drops a batch at once while Tempo is absent, so a Tempo restart loses the spans of its few seconds and nothing queues.
Tempo has no gRPC receiver, because nothing sends gRPC.

## Retention, volume and memory

Tempo keeps 72 hours, enforced by the worker's `backend_worker.compaction.block_retention`, on a 4 GiB local-path volume that ends with `just nuke`.
The scheduler's own `block_retention` key does not delete a block in the pinned version, and the worker's key does.
Tempo has no size key, so only the volume alarm brakes a span flood.
The strategy is `Recreate`: the volume takes one writer, and a second Tempo beside the first would not fit the node.
The sampler keeps every trace, and about 28 spans per job came to 11 to 23 MB an hour in S13.

## Reaching it

```sh
kubectl --context kind-wft -n wft-obs port-forward --address 127.0.0.1 svc/tempo 13200:3200
```

`wft_obs.query.trace` reads `/api/traces/<trace id>` through that forward, and Grafana's `tempo` datasource reads the same service.
