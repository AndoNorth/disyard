# Pyroscope

Profiles of the observability stack: Pyroscope from the pinned image of `deploy/versions.yaml`, in `wft-obs` on the obs node.
The module is group `obs`, profile `ci`, and runs only on a cluster brought up with `WFT_OBS=on`, as the [stack overview](../README.md) says.

## Files

| File | Holds |
| --- | --- |
| `module.yaml` | the module manifest: Pyroscope at 100m of CPU and 256 MiB, requests equal to limits |
| `Tiltfile.module` | loads the objects that `config.py` renders |
| `config.py` | renders every object of the module and the container arguments, and prints either with `render` and `args` |

## Where the profiles come from

Every Python service that calls `wft_obs.setup` pushes profiles to `pyroscope.wft-obs:4040`, as `WFT_PYROSCOPE_ADDRESS` says when the switch is on.
The application name is `wft.<component>`, and `wft_obs.query.profiled` lists the names that hold a profile in a time range.
Grafana reads the same address through its `pyroscope` datasource.
Pyroscope's port is named `metrics`, so the pod scrape finds Pyroscope's own series.

## Retention, volume and memory

Pyroscope runs monolithic (`-target=all`) on filesystem storage under `/data`, and keeps 72 hours (`-retention-period=72h`), the retention of decision 20.
The volume is 2 GiB of local-path storage that ends with `just nuke`.
Pyroscope has no size key, so only the volume alarm brakes a profile flood.
The strategy is `Recreate`: the volume takes one writer, and a second Pyroscope beside the first would not fit the node.
The root filesystem is read-only, so the data path, the storage directory and `/tmp` are the only places the pod writes.

## Reaching it

```sh
kubectl --context kind-wft -n wft-obs port-forward --address 127.0.0.1 svc/pyroscope 14040:4040
```

`wft_obs.query.profiled` reads the API through that forward.
