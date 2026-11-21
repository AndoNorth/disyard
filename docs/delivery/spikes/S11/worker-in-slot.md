# Spike: the worker in the slot (S11)

Run on 2026-11-15.
Owner: the [Kubernetes workers epic](https://github.com/AndoNorth/wasm-dag-forge/issues/10), lane S, filed as [spike issue 375](https://github.com/AndoNorth/wasm-dag-forge/issues/375).
Spec: [S11 in docs/delivery/epics/11-workers.md](../../epics/11-workers.md#s11-the-worker-in-the-slot), which it gates through D.2's simulate pod and `WORKER_RESERVE_MIB` in T.1.
Method: [docs/delivery/method.md](../../method.md).
Throwaway code: [code/](code/), run as `nix develop -c docs/delivery/spikes/S11/code/run.sh [out-dir] [--pools ...] [--simulate-mib N]`.
Every number below is a `figure <name> <value>` line of that run, named `<pool>.<slot MiB>.<run>.<step>.<figure>`.

## Result

**Simulate (1): fail at 384 MiB, pass at 512 MiB. Compile (2): pass. Aggregate (3): pass.**

| Pool | Slot | Runs passed | What decided it |
| --- | --- | --- | --- |
| simulate | 250m, 384 MiB | 0 of 3 | the cold warm-up beside the worker is OOM killed after 31.7 to 32.3 s, every run |
| simulate | 250m, 512 MiB | 3 of 3 | warm-up, warm `echo` at a 128 MiB guest cap and a 96 MiB guest hold, no OOM kill |
| compile | 1 CPU, 1,024 MiB | 3 of 3 | build of the workload library bundle beside the worker, peak working set 458 to 463 MiB |
| aggregate | 1 CPU, 1,024 MiB | 3 of 3 | `digest_list` and `summary_stats` over a result at the staging cap, peak working set 465 to 466 MiB |

`WORKER_RESERVE_MIB` is **256**: the worker's own peak of 164.6 to 165.7 MiB, reached while it collects a result at the 64 MiB staging cap, plus a quarter, in whole 64 MiB steps.
The smallest simulate slot in 128 MiB steps that passes is **512 MiB**, which with that reserve and `GUEST_OVERHEAD_MIB` of 128 gives a guest cap of 128 MiB.
Under Q1 (a) this files the `revisit` child of #2, [issue 380](https://github.com/AndoNorth/wasm-dag-forge/issues/380), which grows the `ci` simulate slot and node and the simulate row of `contracts/temporal/pools.yaml` together, and blocks D.2's simulate pod.

One finding is outside the pass line and outside this epic: the warm-up input of the runtime library does not run on the real workload component, filed as [issue 378](https://github.com/AndoNorth/wasm-dag-forge/issues/378) and described under "Findings".

## Question

Does the worker process fit beside the warm-up and a warm task of the real workload component in each `ci` slot, and how much does it hold?

## Environment

| Item | Value |
| --- | --- |
| Host | WSL2, 20,002 MiB `MemTotal` (`host.mem_total_mib`), 8 CPUs (`host.cpus`), kernel 6.6.87.2 (`host.kernel`), Docker 29.1.3 (`host.docker`) |
| Other work on the host | 1 other container (`host.other_containers`), not touched, 15,815 to 16,684 MiB `MemAvailable` before each run (`host.mem_available_mib.before`) |
| Locks | both `wft-t1` lock files for each run through `wft_itest.Session(containers=2)`, no cluster |
| Containers | one SeaweedFS 4.51 container from `wft_storecheck` with the contract identities, and one capped worker container at a time with the run label, each removed after its run; none left (`containers.left.label_all` 0) |
| Temporal | the dev server of the `temporalio/temporal` 1.9.1 image pin, run as a host process from the binary copied out of a container that was never started, bound to 127.0.0.1 |
| Worker image | `python:3.13.15-slim-trixie` of the `images.python` pin, the hash-pinned third-party packages and workspace wheels of `uv export --package wft-workflow --package wft-aggregate`, `uv` 0.11.21, and the `wasmtime` 48.0.7 and `wasm-tools` 1.262.0 release assets checked against the hashes of `nix/wasm-tools.nix`, with their modification time fixed; 614 MB (`image.bytes`) |
| Tools in the image | Python 3.13.15, `wasmtime` 48.0.7, `wasm-tools` 1.262.0, `uv` 0.11.21, `componentize-py` 0.25.1, `temporalio` 1.35.0 (`tool.*`) |
| Worker container | `docker run --memory <slot> --memory-swap <slot> --cpus <slot cpu> --read-only --network host`, the invoking user, a host directory at `/var/wft` for scratch, the module cache, `HOME`, `TMPDIR`, `UV_CACHE_DIR` and `XDG_CACHE_HOME` (D13), and the pool's store identity, and only it (the aggregate identity also, for the aggregate child), read only |

## Method

- The component is the workload library bundle of `python -m wft_workloads.bundle`, built on the host by `wft_compile` with entry `wft_workloads.main`: 18.2 to 18.3 MiB (`seed.component_mib`), cache key `c523632e…` (`seed.cache_key`), and uploaded as a `component`; the bundle is uploaded as a `source`.
- The aggregate input is one result at the staging cap, 67,108,852 of 67,108,864 bytes (`seed.result_bytes`, `seed.result_cap_bytes`), 2,684,349 of the smallest valid row (`seed.result_rows`), as the aggregate library's own memory test builds it, behind an index put with `put_index`.
- The stand-in worker, [code/worker.py](code/worker.py), is the shape of 0.4: one `temporalio` activity worker on its own queue with `max_concurrent_activities=1`, the client built with `event_data_converter` and `tracing_interceptor()`, the core imports of D1 (`wft_workflow`, `wft_artifactclient`, `wft_events`, `wft_keys`, `wft_stages`, `wft_temporal`) and the pool's own (`wft_runtime` on simulate, `wft_compile.limits` on compile), an `ArtifactClient` of its pool's identity, a heartbeat from its own loop every 10 s, store transfers in a thread, every child in its own process group with stdout and stderr merged and read in 65,536-byte chunks into a tail, and a log tail uploaded after each step.
- A host workflow drives each step as one `s11_task` activity on the container's queue with a single attempt.
- Per pool and run, in one fresh container:
  - simulate: stage the component and upload a tail, then collect a result at the staging cap with `put_attempt`, which fixes the worker's own peak and so `WORKER_RESERVE_MIB`; then the cold warm-up; then a warm `echo` with the level 0 vector input at `memory_mib = slot - WORKER_RESERVE_MIB` (0.4); then, beyond the pass line, a warm `mem_hold` of the guest cap less 32 MiB;
  - compile: stage the bundle, then `python -m wft_compile.limits fetch` and `python -m wft_compile build --find-links DIR`, then upload `component.wasm` and `build-info.json`;
  - aggregate: stage the index, then `python -m wft_aggregate run` with `digest_list`, then with `summary_stats`, each uploading `summary.json`.
- The worker's own resident memory is `VmRSS` and its peak `VmHWM` of the worker process, read from the host after each step.
- The container is read as in S6: the peak working set (`memory.current` less `inactive_file`) and the peak anon sampled every 10 ms per step, `memory.peak`, and the `oom_kill` count of `memory.events`.
- A run passes when every step completes with exit 0 and the expected class, and the container records no OOM kill.

## Measured numbers

### Simulate, 250m

| Figure | 384 MiB | 512 MiB | Names |
| --- | --- | --- | --- |
| Worker idle, polling, resident | 111.8 to 113.0 MiB | 112.3 to 112.9 MiB | `.idle.worker_rss_mib` |
| Worker peak after staging the component | 115.7 to 116.5 MiB | 116.0 to 116.3 MiB | `.stage.worker_hwm_mib` |
| Worker peak after collecting a 64 MiB result | 164.6 to 165.7 MiB | 165.0 to 165.6 MiB | `.upload.worker_hwm_mib` |
| Worker resident after that upload | 131.8 to 132.9 MiB | 132.2 to 132.7 MiB | `.upload.worker_rss_mib` |
| `WORKER_RESERVE_MIB` | 256 | 256 | `.worker_reserve_mib` |
| Staging the component, 18.3 MiB | 0.04 to 0.17 s | 0.08 to 0.14 s | `.stage.stage_s`, `.warm_up.stage_s` |
| Collecting the 64 MiB result | 1.00 to 1.09 s | 0.95 to 1.10 s | `.upload.stage_s` |
| Warm-up | **OOM killed** (exit -9) after 31.7 to 32.3 s | exit 0 in 32.6 to 33.2 s | `.warm_up.child.exit`, `.warm_up.child.wall_s` |
| Warm-up peak working set, peak anon | 380 to 384, 378 to 382 MiB | 420 to 424, 413 to 416 MiB | `.warm_up.peak_ws_mib`, `.warm_up.peak_anon_mib` |
| Guest cap that the reserve gives | 0 MiB, refused before `wasmtime` | 128 MiB | `.echo.guest_cap_mib` |
| Warm `echo` | not run | class `ok` in 0.49 to 0.51 s, peak working set 166 to 171 MiB | `.echo.class`, `.echo.child.wall_s`, `.echo.peak_ws_mib` |
| Warm `mem_hold` of 96 MiB (probe) | not run | class `ok`, peak working set 265 MiB, peak anon 233 to 234 MiB | `.hold.*` |
| `memory.peak` | 384 MiB | 450 to 454 MiB | `.memory_peak_mib` |
| OOM kills | 1 per run | 0 | `.oom_kill` |
| Runs passed | 0 of 3 | 3 of 3 | `simulate.runs_passed` |

The warm-up's peak working set at 512 MiB is the worker's resident 132 MiB and about 290 MiB of `wasmtime` compiling, the first-run figure of S6 (290 to 326 MiB), so a 384 MiB slot has no room for any worker beside it.
The worker's peak while it uploads comes after the guest has exited, so it never meets the warm-up's peak, but the worker keeps about 17 MiB of it resident for the rest of the pod's life, and the sizing rule takes the peak.
At 512 MiB the tightest warm-up left 88 MiB below the cap.

### Compile, 1 CPU and 1,024 MiB

| Figure | Value | Names |
| --- | --- | --- |
| Worker idle, polling, resident | 113.6 to 114.0 MiB | `compile.1024.<n>.idle.worker_rss_mib` |
| Worker peak, after uploading the 18.3 MiB component | 150.3 to 150.6 MiB | `compile.1024.<n>.worker_hwm_mib` |
| Fetch, then build | 0.19 to 0.20 s, then 7.65 to 7.79 s, both exit 0 | `.build.fetch.wall_s`, `.build.child.wall_s` |
| Peak working set, peak anon | 458 to 463 MiB, 440 to 442 MiB | `.build.peak_ws_mib`, `.build.peak_anon_mib` |
| Largest child resident | 368.7 to 370.6 MiB | `.build.children_maxrss_mib` |
| `memory.peak` | 473 to 479 MiB | `.memory_peak_mib` |
| OOM kills, runs passed | 0, 3 of 3 | `.oom_kill`, `compile.runs_passed` |

### Aggregate, 1 CPU and 1,024 MiB

| Figure | `digest_list` | `summary_stats` | Names |
| --- | --- | --- | --- |
| Child time, exit | 26.6 to 27.9 s, exit 0 | 11.7 to 12.4 s, exit 0 | `.<aggregation>.child.wall_s`, `.child.exit` |
| Peak working set, peak anon | 465 to 466, 392 to 393 MiB | 402 to 405, 329 to 332 MiB | `.<aggregation>.peak_ws_mib`, `.peak_anon_mib` |
| Largest child resident | 347.0 to 347.3 MiB | the same child figure | `.children_maxrss_mib` |

The worker idles at 112.4 to 113.0 MiB and peaks at 114.2 to 114.8 MiB (`aggregate.1024.<n>.worker_hwm_mib`), `memory.peak` is 466 MiB, with no OOM kill, 3 of 3.

## Figures for #9 (D18)

| Pool | Worker idle | Worker peak | Staging | Warm-up | Pod peak working set | Pod peak anon | Heartbeat |
| --- | --- | --- | --- | --- | --- | --- | --- |
| compile | 114 MiB | 151 MiB (18 MiB component upload) | under 0.1 s for the bundle | none | 463 MiB | 442 MiB | every 10 s, as configured |
| simulate, 512 MiB | 113 MiB | 166 MiB (64 MiB result upload) | 0.14 s for the component, 1.1 s for a 64 MiB result | 33.2 s at 250m | 424 MiB | 416 MiB | every 10 s, as configured |
| aggregate | 113 MiB | 115 MiB | the child stages its own inputs | none | 466 MiB | 393 MiB | every 10 s, as configured |

The cancel latency and the drain time are A.1's and A.2's to print, since this stand-in implements neither.
Staging and upload times are against SeaweedFS on the same host and are lower bounds for the cluster.

## Findings

1. **Simulate needs a 512 MiB slot.**
   A worker of 112 to 166 MiB beside a first-run compile of about 290 MiB does not fit 384 MiB, as Q1 expected, and 512 MiB holds it with 88 MiB to spare at the worst run.
   [Issue 380](https://github.com/AndoNorth/wasm-dag-forge/issues/380) grows the slot to 512 MiB and the node from 768 to 896 MiB, and the simulate row of `pools.yaml` with them.
2. **`WORKER_RESERVE_MIB` is 256, not the 128 that 0.4 estimated.**
   Staging alone gives a peak of 116 MiB and a reserve of 192, but collecting a result at the staging cap through `put_attempt` lifts the peak to 166 MiB, so the guest cap at 512 MiB is 128 MiB.
   T.1 takes 256, and a later move of the upload out of the worker's process, or a smaller part size than the client's 8 MiB, is the place to win it back.
3. **The runtime's warm-up input does not run on the real workload component** ([issue 378](https://github.com/AndoNorth/wasm-dag-forge/issues/378)).
   `wft_runtime.command.NOOP` carries `args: {}`, and the workload library's `noop` requires `heartbeat_every`, so the guest raises after the compile and `wasmtime` exits 134.
   The compile still fills the module cache, so the next run is warm, but `wft_runtime.run.warm` never records the marker, and a worker that gates on `warmed` repeats the warm-up for every task.
   The numbers above give the warm-up the input `{"args": {"heartbeat_every": 1}, "seed": 0, "workload": "noop"}`, which is what the fix would send; a first run of the same sequence with the runtime's own input ended every warm-up with exit 134 and no OOM kill at 512 MiB.
4. **Compile and aggregate fit with room.**
   Their worst peak working set is 466 MiB of 1,024 MiB with the worker beside the child, so no sizing change follows, and no needs-decision is raised for (2) or (3).

## Caveats and what was not measured

- One host and one run of each sequence for the tables above.
  Before them, a run at the preset sizes fixed the reserve from staging alone (192 MiB) and also failed simulate at 384 MiB on its warm-up 3 of 3, and a run at 512 MiB with the runtime's own warm-up input surfaced finding 3; their compile and aggregate figures are the ones above.
- Docker on WSL2 stands in for a kind node, with the same cgroup v2 memory accounting and no kubelet; the pod adds the kubelet's own view of the working set, which these figures match by construction.
- The worker is a stand-in of the shape of 0.4, not `wft_worker`; the real worker adds the runner, the classification and the contract models in use, and its imports are the ones measured here.
- The worker's idle resident memory counts shared pages of the image's Python files, so its `VmRSS` (112 MiB) is above the container's idle working set (85 to 88 MiB, `.idle.peak_ws_mib`).
- The compile worker's peak follows an 18 MiB component; a component near the 64 MiB cap would lift it about as the result upload lifts the simulate worker, which the 1,024 MiB slot absorbs.
- Two builds of the same bundle gave components of 19,112,999 to 19,160,192 bytes with the same cache key (`seed.component_bytes`), so the build is not byte-reproducible; the cache keys on its inputs, so this does not change the cache.
- `wasmtime` compiles with as many threads as the host has CPUs, 8 here, as S6 noted.
- Not measured: a guest that holds its full 128 MiB cap at 512 MiB (the probe held 96 MiB), cancel and drain, and several tasks in a row on one pod past the module cache of one component.
