# Pool workers

The pool worker service, `python -m wft_worker --pool <pool>`, deployed in `wft-workers` as one module per pool: `compile-worker`, `simulate-worker` and `aggregate-worker`.
Each runs one task at a time on its pool's unversioned queue, so it serves the workflows of every build.
The modules are in profile `ci` only, because the compile module needs the package mirror, which only `ci` runs.

## Files

| File | Holds |
| --- | --- |
| `Dockerfile`, `image.sh` | the image of each pool: the wheels of `wft-worker` with the pool's extra over the hash-pinned packages of `uv.lock`, and the pool's release binaries from `nix/wasm-tools.nix` |
| `pool.star` | the module of one pool: its image, Deployment, store Secret and NetworkPolicy |
| `<pool>/module.yaml`, `<pool>/Tiltfile.module` | the module manifest of each pool, with the pod at the pool's slot of `deploy/presets/slots/ci.yaml`, and the call into `pool.star` |
| `test_image.py`, `test_render.py` | the image tests, run by `just itest`, and the render tests, offline through `tilt alpha tiltfile-result` |

## The pod

One Deployment `<pool>-worker` with one replica and the `Recreate` strategy, because a pool node holds one slot and a surge pod does not fit.
Requests equal limits equal the slot, and the service reads the memory limit as `WFT_SLOT_MEMORY_MIB` from the downward API.
The pod runs as user 65532 with a read-only root filesystem, no service account token and the runtime's default seccomp profile.

A disk `emptyDir` of at most 1 GiB is mounted at `/var/wft` and holds the task scratch, the `wasmtime` module cache, `HOME`, `TMPDIR`, the caches of `uv` and `XDG_CACHE_HOME`, and the ready file.
An init container creates the directories the tools write to.
The cache outlives a container restart inside the pod.

On `SIGTERM` the service stops polling and gives the task in flight 30 s, then fails it as `worker_shutdown`.
`terminationGracePeriodSeconds` is 75, which covers the drain, the group kill of the child and the upload of its log tail.

## Store identity and database

The Secret `<pool>-worker-store` holds `credentials.json` with the pool's own store identity only, taken from `objectstore.credentials.json` in the state directory of the cluster scripts.
No database login, DSN or Secret reaches `wft-workers`.

## Egress

A NetworkPolicy per pool allows no ingress, and egress to DNS, the Temporal frontend and the object store only, plus the package mirror from the compile pod.
The probe prints what the pod reaches, and exits 0 when that is what the policy allows:

```sh
kubectl --context kind-wft -n wft-workers exec deploy/compile-worker -- python -m wft_worker.probe
```

## Readiness and logs

The service creates `/var/wft/ready` once it polls, and the readiness probe tests for it.
The module is ready when the Deployment is `Available`.
The log names the queue polled, unversioned:

```sh
kubectl --context kind-wft -n wft-workers logs deploy/<pool>-worker
```

A real job through the deployed workers:

```sh
uv run --frozen python -m wft_workflow.harness --kube-context kind-wft smoke --pools real --jobs 5
```
