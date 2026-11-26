# Operator runbook

How to bring the system up on a laptop, prove it works, look at its health and tear it down, with nothing left behind.
Every command runs from the repository root.
`just` exists only inside the dev shell, so each command is written `nix develop -c just <recipe>`, or enter the shell once with `nix develop` and drop the prefix.

Host prerequisites and the recipe table are in [dev setup](../dev-setup.md), and the WSL2 settings that keep the cluster alive are in the [cluster host guide](../cluster-host.md).
This page does not repeat them.

## The cluster rules

| Rule | Why | How it is enforced |
| --- | --- | --- |
| One cluster at a time | the kind cluster is always named `dsy`, and it binds fixed ports on `127.0.0.1` | `just up` refuses (exit 4) a cluster it did not start, or one started for another preset or observability value |
| The cluster lock | two recipes touching the cluster at once corrupt a run | every cluster recipe takes `/tmp/dsy-cluster.lock` and waits up to 900 s, unless the caller already holds it (`DSY_CLUSTER_LOCK_HELD`); `tilt/run.sh` locks for `ci` and `soak`, and `dev` stays unlocked |
| Enough memory | the nodes are containers inside the WSL VM, and a short host gets nodes OOM-killed | `just up` refuses (exit 4) when the preset needs more memory than the host has |
| One preset per cluster | node sizes are fixed when kind creates a node | `just nuke` first, then `just up` for another preset |
| One observability value per cluster | the observability node is sized at creation | `just nuke` first, then `just up` with the other `DSY_OBS` |
| Loopback only | no published port may be reachable from the network | `just cluster-verify` checks every host port listens on `127.0.0.1` |

The lock is released when a recipe ends, so `just up`, `just cluster-verify` and `just smoke` can follow each other.
Another person's or agent's recipe holding the lock makes yours wait, and a wait that runs out exits 4.
Check who holds it before assuming a hang, which prints nothing when the lock is free:

```sh
fuser -v /tmp/dsy-cluster.lock
```

### Presets and memory

`DSY_PRESET` selects the preset, `ci` when unset.

| Preset | Memory needed | With `DSY_OBS=on` | Nodes | Use |
| --- | --- | --- | --- | --- |
| `ci` | 12,032 MiB | 15,488 MiB | one each of control plane, infra, obs, compile, simulate and aggregate | the default, and the only preset `just smoke` accepts |
| `dev` | 12,288 MiB | not available | the same six, with more CPU | iterating with live update, `scripts/tilt/run.sh dev` |
| `soak` | 15,104 MiB | 17,664 MiB | `ci` with two compile and two simulate nodes | long runs |

The figures are the `memory_mib` and `obs_memory_mib` keys of [deploy/presets/](../../deploy/presets/), and `just doctor` compares them with the host (`memory-vs-preset`).
Pod slots are the same on every preset, so a preset changes how many pods run side by side and never how large one is.
A resized preset needs `just nuke` first, because the kubelet reservation is written when a node is created and `just up` ends in a `MISMATCH` otherwise.

### Measured cost of a live session

One session on 2026-11-24: `DSY_OBS=on just up`, `scripts/tilt/run.sh ci` twice (the second after one commit changed the workflow image), four `just smoke` runs, a browser check of six dashboards, and `just nuke`.
The host was an AMD Ryzen 5 7500F (6 cores, 8 logical CPUs in WSL2) with MemTotal 20,482,272 kB, shared with other agent sessions that ran tests and containers at the same time.
Every figure below is a measurement from that one session, and the host-wide ones include the work of the other sessions.

| Phase | Time |
| --- | --- |
| `just up` | 58 s |
| `scripts/tilt/run.sh ci`, first deploy, images cached | 234 s |
| `scripts/tilt/run.sh ci`, second deploy, one image rebuilt | 161 s |
| `just smoke`, the four runs as the gate printed them | 78 s, 67 s, 28 s, 27 s |
| Six dashboard screenshots and a scale-to-zero of Tempo | 78 s |
| `just nuke` | 13 s |
| Cluster lock held, `flock` taken to released | 2,542 s (42 min 22 s), of which about 14 min waited on the browser |

| Resource | Figure |
| --- | --- |
| `MemAvailable` before `just up` | 16,806,564 kB |
| `MemAvailable` over 59 samples, every 30 s from the end of `just up` to the teardown | maximum 15,706,924 kB, minimum 12,579,044 kB |
| Load average over one minute, same samples | maximum 11.7, minimum 1.23 |
| Node container memory, `docker stats` after the last smoke | control plane 744 MiB, `dsy-worker` 2,087 MiB, `dsy-worker2` 1,318 MiB, `dsy-worker3` 373 MiB, `dsy-worker4` 403 MiB, `dsy-worker5` 421 MiB, 5,346 MiB in all |
| `docker system df`, first sample to peak | images 76.28 GB to 80.28 GB, local volumes 10.05 GB to 23.09 GB, build cache 65.44 GB to 72.90 GB |

`kubectl top` is not available, because the cluster has no metrics API.
The first `docker system df` sample was taken after `just up`, so the images and volumes of the nodes are already in it.

## Bring up

1. Check the host.

   ```sh
   nix develop -c just doctor
   ```

   Every line must be `ok` or `warn`, see [`just doctor`](../dev-setup.md#just-doctor).
   A `warn memory-vs-preset` with `DSY_OBS=on` means the host fits the cluster only with the observability switch off.

2. Check the ports.

   ```sh
   nix develop -c just ports
   ```

   It exits 0 when nothing holds a port the cluster binds, and 1 with the holder otherwise.
   `just up` runs the same check before it creates a cluster.

3. Create the cluster.

   ```sh
   nix develop -c just up
   ```

   It creates the six kind nodes and the local registry `dsy-registry`, applies the namespaces and quotas, and ends with `cluster dsy ready: preset ci, DSY_OBS=off, 6 nodes`.
   Running it again with nothing changed leaves every container as it was.

4. Deploy the modules.

   ```sh
   scripts/tilt/run.sh ci
   ```

   `tilt ci` builds the images, deploys every module of the profile and exits when all resources are ready, or non-zero when one fails.
   `just up` creates the cluster and does not deploy anything, so a cluster that passed step 3 holds no workload until this step.
   `scripts/tilt/run.sh` takes the observability value from the cluster's record, so it needs no `DSY_OBS`.

5. Check the cluster and then prove the platform, see [Check health](#check-health).

### Observability on

The observability stack (Prometheus and its alert rules, Loki, Tempo, Pyroscope, Grafana) runs only when the cluster is created with the switch on:

```sh
DSY_OBS=on nix develop -c just up
scripts/tilt/run.sh ci
```

A cluster that exists with the switch off needs `nix develop -c just nuke` before this.
It needs the larger memory figure above, and GitHub CI never deploys it.
With it on, these answer on the host:

| What | Address |
| --- | --- |
| Grafana | `http://127.0.0.1:13000` |
| Temporal UI | `http://127.0.0.1:18233` |
| Dispatch API | `http://127.0.0.1:18081` |
| Control API | `http://127.0.0.1:18080` |

Prometheus and the broker have no host port, and are reached through a `kubectl port-forward`, see [alert response](alerts.md#reading-the-firing-alerts).

## Run a job end to end

The proven path is the smoke gate, which uploads the workload library, submits one job, waits for it to end `succeeded`, and checks its result against the known answer:

```sh
nix develop -c just smoke
```

It needs preset `ci`, attaches to the running cluster, and never starts or stops one.
Its 11 rows and what each proves are in [the validation matrix](validation-matrix.md#the-smoke-rows).
The job it submits is one `echo` job for the tenant `acme`.

To follow a job after it was submitted, ask the control API for its status, with the job id that `job-submit` printed:

```sh
curl -s http://127.0.0.1:18080/v1/tenants/acme/jobs/<job id>
```

The Temporal UI shows the same job at `http://127.0.0.1:18233/namespaces/dsy/workflows/job-<job id>`.
The control actions are `cancel`, `terminate`, `pause` and `resume`, posted to `/v1/tenants/acme/jobs/<job id>/<action>` or, for a whole batch, to `/v1/tenants/acme/batches/<batch key>/<action>`.
The routes, bodies and reason codes are in [the API contract](../../contracts/api/README.md).

More drivers, each needing the cluster up and the modules deployed:

| Driver | What it runs |
| --- | --- |
| `uv run --frozen python -m dsy_dispatchapi.smoke --scenario submit --jobs 3` | the API scenarios `upload`, `submit`, `cancel-before-start`, `run`, `pause-resume`, `cancel-running` and `restart` |
| `uv run --frozen python -m dsy_workflow.harness --kube-context kind-dsy smoke --pools real --jobs 5` | real jobs through the deployed pool workers |

A job that is stuck has a path through [triage by symptom](triage.md).

## Check health

```sh
nix develop -c just cluster-verify
```

It is read-only and prints one `ok <check>` or `FAIL <check>` line each for the preset, pod slots, module budget, nodes, node limits, quotas, module readiness, live requests, pressure and loopback ports, then a count line.
It exits 0 when every check is ok, 1 when one failed, and 4 when there is no cluster `dsy` or it was started for another preset.

`just smoke` starts with the same checks (row `cluster-verify`) and then drives a job, so on a deployed cluster `just smoke` alone is the whole health check.

For a quick look at the pods:

```sh
kubectl --context kind-dsy get pods -A
```

With the observability stack on, firing alerts are at `http://127.0.0.1:19090/alerts` after a forward, and each has an entry in [alert response](alerts.md).
The `cluster-health` and `stack-health` dashboards in Grafana show nodes, pods, memory and volumes.

## Tear down

| To | Run | Keeps |
| --- | --- | --- |
| Pause for the night | `nix develop -c just down` | the cluster, the registry and the host data directory, and `just up` starts the same containers again |
| Start clean, or change the preset or the switch | `nix develop -c just nuke` | nothing of the cluster: it deletes the cluster, the registry and the host data directory |

`just nuke` is safe to repeat, and it removes only a data directory that a preset in `deploy/presets/` names.
It reads the preset from the cluster's own record, so `DSY_PRESET` is only needed when that record is gone.

### Confirm nothing is left behind

```sh
kind get clusters
docker ps -a --filter label=io.x-k8s.kind.cluster=dsy --format '{{.Names}}'
docker ps -a --filter name=dsy-registry --format '{{.Names}}'
ls ~/.local/share/dsy
ls ~/.local/state/disyard/cluster.owner ~/.local/state/disyard/cluster.obs
nix develop -c just ports
```

After `just nuke` the first command prints `No kind clusters found.`, the two `docker ps` commands print nothing, `ls ~/.local/share/dsy` shows no directory of the preset you ran (a later render or test run can recreate it empty), the two state files are reported missing, and `just ports` exits 0 unless something else holds a port.
A container that `just ports` labels a leftover is named in its output and is never removed by it, so remove it by hand with `docker rm -f <name>`.

What survives on purpose, because it is not part of the cluster:

| What | Where | Remove with |
| --- | --- | --- |
| Generated credentials of the services | `~/.local/state/disyard/` (`$DSY_STATE_DIR`, else `$XDG_STATE_HOME/disyard`) | `rm -r` the directory, only with no cluster running |
| The build download cache | `~/.cache/disyard/build` (`$DSY_BUILD_CACHE`) | `rm -r`, always safe |
| The cluster lock file | `/tmp/dsy-cluster.lock` | harmless, leave it |

## Host sleep and restart

Windows sleep, a Windows update reboot and `wsl --shutdown` stop the VM and every node without a graceful shutdown.
Run `nix develop -c just up` after the host is back: it starts the stopped node containers and waits for the API server.
Then run `nix develop -c just cluster-verify`, because pods that restarted show in [DsyPodRestartUnexpected](alerts.md#dsypodrestartunexpected) and the data on the host data directory survives.
The WSL settings that keep the VM running are in [the cluster host guide](../cluster-host.md#what-no-setting-covers).

## Where the names are

A project rename is planned, so the names live in a few places.
The cluster, its kubectl context and the namespaces are the only names a command in these runbooks repeats.

| Name | Meaning | Defined in |
| --- | --- | --- |
| `dsy` | the kind cluster, and the prefix of namespaces and environment variables | [scripts/cluster/](../../scripts/cluster/) and [deploy/presets/](../../deploy/presets/) |
| `kind-dsy` | the kubectl context | `context=` in [scripts/up.sh](../../scripts/up.sh) |
| `dsy-infra`, `dsy-control`, `dsy-workers`, `dsy-obs` | the namespaces | [contracts/topology/topology.yaml](../../contracts/topology/topology.yaml) |
| `acme` | the first tenant | [contracts/tenancy/](../../contracts/tenancy/README.md) |
