# Placement

The placement service, `python -m wft_placement`, deployed in `wft-control` on the infra pool.
It answers the `place` call of every workflow build with a pool queue, and reads a snapshot of the pools from the Kubernetes API, as [the package README](../../packages/placement/README.md) says.
Module: `placement`.

## Files

| File | Holds |
| --- | --- |
| `module.yaml` | the module manifest: the pod at 50m and 250m of CPU and 128 MiB of memory, requests equal to limits in memory |
| `Tiltfile.module` | the image build, the service account and its read-only cluster role, the Deployment and the ClusterIP Service |
| `image.sh`, `Dockerfile` | the image: wheels of the workspace packages built on the host, over the third-party packages that `uv.lock` pins by hash, on the pinned `python` image of `deploy/versions.yaml` |
| `test_render.py` | the render tests, offline through `tilt alpha tiltfile-result` |

To build the image by hand:

```sh
deploy/placement/image.sh <ref>
```

## Shape

One replica with strategy `Recreate`, because the service is stateless and one pod serves every live workflow build.
The service is unversioned: it carries no build id, and a change to its wire models is additive only, rolled out with this image first and the workflow build that uses it after.
The workflow workers reach it at `http://placement.wft-control.svc:8080` through `WFT_PLACEMENT_URL`, and `place` retries across a restart.

## Access to the Kubernetes API

The pod runs as the service account `placement`, and of the service images built in this tree it is the only pod that mounts a token.
The ClusterRole `wft-placement-read` allows `get` and `list` on `nodes` and `pods` and nothing else, and its binding is cluster wide, because nodes are cluster scoped and the daemon pods on pool nodes count against them.

## Configuration

The service reads only its environment.

| Variable | From |
| --- | --- |
| `WFT_PLACEMENT_LISTEN` | `0.0.0.0:8080`, the port of the Service |
| `WFT_READY_FILE` | `/tmp/wft-ready`, which the readiness probe tests |
| `KUBERNETES_SERVICE_HOST`, `KUBERNETES_SERVICE_PORT` | set by the kubelet |

`WFT_SNAPSHOT_EVERY_S` stays unset, so the service reads the pools every 30 s.

## Looking at it

```sh
kubectl --context kind-wft -n wft-control logs deploy/placement
kubectl --context kind-wft -n wft-control exec deploy/placement -- python -m wft_placement.show
```

The log holds one line per answer, `placed ...` or `unschedulable ...`, and `show` prints each pool with its ready workers and free capacity.
