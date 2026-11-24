# State inputs exporter

The state of the app database as Prometheus gauges: `python -m dsy_obs.inputs` from [packages/obs](../../../packages/obs/README.md), in `dsy-obs` on the obs node.
The module is group `obs`, profile `ci`, and runs only on a cluster brought up with `DSY_OBS=on`, as the [stack overview](../README.md) says.

## Files

| File | Holds |
| --- | --- |
| `module.yaml` | the module manifest: the exporter at 20m of CPU and 96 MiB, requests equal to limits, and the login resource |
| `Tiltfile.module` | the Deployment, the Secret of the login, and the host-side resource that creates the login |
| `logins.py`, `logins.sh` | create or check the login `state_inputs` in the group role `dsy_reader`, as admission does for its own, and render its Secret |
| `Dockerfile`, `image.sh` | build the image from the workspace wheels of `dsy-obs` and its `stateops` extra over the hash-pinned packages of `uv.lock`, on the pinned Python base |

## What it serves

Every 60 s the exporter reads the inputs of `contracts/state/inputs.yaml` of kind `sql`, `sql_labelled` and `invariants`, and serves each as the gauge `dsy_state_<name>` on the port named `metrics` (9464), with `_bytes` or `_seconds` appended when the unit is one of those and the name does not already end in it.
A labelled input has one series per label value.
The two `store` inputs are not read: they need `kubectl exec` into the store pod and the store credentials, and the store's own metrics carry the same figures.

An input that fails keeps its last value and is counted in the process and logged as an error line.
The pod is ready once a pass has reached the database.

## The login

`logins.sh apply` creates the login with a password generated into the cluster state directory on first use, and `check` exits 1 when the login differs.
The login is a member of `dsy_reader` only, which holds `SELECT` on the job, event, artifact and reaper tables and nothing else.
Its DSN is the Secret `state-inputs-logins` in `dsy-obs`.
