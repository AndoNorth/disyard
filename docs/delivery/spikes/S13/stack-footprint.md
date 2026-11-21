# Spike: footprint of the observability stack (S13)

Run on 2026-11-21.
Owner: the observability epic, lane S.
Breakdown and method: [docs/delivery/epics/18-observability.md](../../epics/18-observability.md) and [docs/delivery/method.md](../../method.md).
Throwaway code: [code/](code/), applied with `code/apply.sh` and `code/wire.py`.
Raw data (snapshots, polls, logs) stays outside the repository, and every figure below is read from it.

## Result

**Fail.**
The pass line has four parts, and the stack misses three of them.

| Pass line part | Result |
| --- | --- |
| No obs container restarts or is OOM killed | Fail: Grafana at 128 MiB was OOM killed twice and exited once, and Tempo was OOM killed 31 times at 256 MiB, 5 times at 384 MiB and 2 times at 512 MiB |
| Every obs container peaks at most 80 percent of its D9 limit | Fail: Grafana 100 percent of 128 MiB, Tempo 89 to 100 percent, Prometheus 84 percent at 384 MiB under the harness |
| Every service and worker container stays at most 80 percent of its limit with the wiring on | Fail: admission 85 percent, the workflow worker 95 to 98 percent under the harness, the simulate pod 91 percent at the staging-cap job (88 percent with the wiring off) |
| Bytes per hour at retention fit a cap and volume, and `budget.sh ci` exits 0 | Storage fits (see below). `budget.sh` exits 0 only with the shipped figures: the obs modules are not in the repository yet, so it counts 50 MiB on the obs node. The figures that pass the 80 percent line sum to about 2,368 MiB against 1,904 MiB allocatable, so they would not exit 0 |

The stack does not fit the 2,304 MiB obs node of the `ci` preset at an 80 percent line.
Decision 22's node size returns to the captain, with the numbers in this document.
Pyroscope is the first component to drop: without it the figures sum to about 2,112 MiB, which still exceeds 1,904 MiB, so the node needs 2,560 MiB or the 80 percent line has to move.

## Question

Does the whole stack of D9, Tempo and Pyroscope included, run on the `ci` obs node at its `obs_stack` estimate, how fast does its storage grow, and what do the trace and profile wiring cost the service and worker pods, the simulate worker at its staging-cap peak included?

## Method

- `WFT_OBS=on just up` on preset `ci`, under `/tmp/wft-cluster.lock`. The obs node is 1.5 CPU and 2,304 MiB.
- The eight obs Deployments of `code/stack.yaml` and `code/grafana.yaml`, at D9's limits, with requests equal to limits, strategy `Recreate`, and a scrape of every D1 target at 30 s.
- Prometheus keeps 10 days and 4 GB, Loki and Tempo and Pyroscope take the retention of decision 20.
- The wiring (`code/payload`, `code/wire.py`): an init container copies the OpenTelemetry SDK and `pyroscope-io` into an emptyDir, `PYTHONPATH` loads a `sitecustomize` that starts the tracer (sampler `ParentBased(TraceIdRatioBased(1.0))`), the OTLP log handler and the profiler, and a NetworkPolicy allows egress to the collector and Pyroscope. The services and workers are not changed, so the wiring is a stand-in for H.3.
- Arms: three smoke runs, a compile that misses the cache, and the staging-cap job (a 67,108,548 byte `io_churn` result, the peak `WORKER_RESERVE_MIB` is sized for), once with the wiring off and once on.
- Load: the workflow harness (`smoke --jobs 2000`, fake pools) for two batches (2,897 s and 2,595 s, 4,000 of 4,000 finished, 0 failed, 13 infra retries), with every panel query of every provisioned dashboard run once a minute (1,617 queries, 0 failed).
- Two more harness runs of 700 jobs each tested Tempo at 384 MiB and at 512 MiB (the second with Pyroscope scaled to zero to free node memory).
- Peaks: cgroup v2 `memory.peak`, `memory.current` less `inactive_file` polled every second, and cAdvisor through Prometheus (`max_over_time` of the working set over the load window). `memory.peak` includes page cache, so it is an upper bound on the working set.
- Volumes: `du -sb` of each local-path volume on the obs node.

## Peak working set and throttled share

Window: the 3.4 hours from the first harness batch to the end of the second, then idle.
The throttled share is `container_cpu_cfs_throttled_periods_total` over `container_cpu_cfs_periods_total`.

| Container | Limit | Peak working set | Share of limit | Throttled | Mean CPU | CPU limit | Restarts |
| --- | --- | --- | --- | --- | --- | --- | --- |
| prometheus | 384 MiB | 285 MiB (321 MiB in the later 700-job run, 362 MiB `memory.peak`) | 74 to 84 percent | 1 percent | 7 to 9m | 200m | 0 |
| loki | 256 MiB | 98 MiB (106 MiB `memory.peak`) | 38 percent | 0 percent | 7m | 100m | 0 |
| tempo | 256 MiB | 227 MiB until killed | 89 to 100 percent | 99 percent | 0 to 49m | 100m | 31 OOM kills |
| pyroscope | 256 MiB | 123 MiB (152 MiB `memory.peak`) | 48 percent | 3 percent | 9m | 100m | 0 |
| grafana | 128 MiB | 128 MiB, at the limit | 100 percent | 75 percent | not stable | 50m | 2 OOM kills and 1 exit 1 |
| grafana | 256 MiB (measured) | 243 MiB (260 MiB `memory.peak`) | 95 percent | 18 percent | 23m | 100m | 0 |
| collector | 128 MiB | 90 MiB (109 MiB `memory.peak`) | 70 to 85 percent | 1 percent | 3 to 5m | 50m | 0 |
| kube-state-metrics | 64 MiB | 24 MiB | 37 percent | 9 percent | 1m | 20m | 0 |
| state-inputs | 96 MiB | 43 MiB | 45 percent | 34 to 46 percent | 0m | 20m | 0 |

Tempo at 384 MiB reached 383 MiB (100 percent) and was killed 5 times in 15 minutes of a 700-job run.
Tempo at 512 MiB reached 425 MiB (83 percent), 99 percent throttled at 100m, and was killed twice in 18 minutes.
The first kill of each restart was often the replay of the write-ahead log on start, which is why a pod that is Ready still counts restarts.
Grafana's first start takes about 3 minutes and its plugin directory holds 249 MB.
Grafana at 256 MiB still sits at 95 percent, so it needs at least 336 MiB for the 80 percent line.

## Service and worker pods, wiring off and on

Peak is `memory.peak` of the container, read from the node, so the percentages are upper bounds.
Every pod was restarted before each arm.

| Pod | Limit | Off (smoke, compile, staging cap) | On (same) | On after the harness |
| --- | --- | --- | --- | --- |
| admission | 128 MiB | 78 MiB, 61 percent | 89 MiB, 69 percent | 109 MiB, 85 percent |
| dispatch | 256 MiB | 93 MiB, 37 percent | 132 MiB, 52 percent | 132 MiB, 52 percent |
| placement | 128 MiB | 57 MiB, 44 percent | 61 MiB, 47 percent | 63 MiB, 49 percent |
| workflow worker | 256 MiB | 99 MiB, 39 percent | 113 MiB, 44 percent | 250 MiB, 98 percent, 89 percent throttled |
| aggregate worker | 1,024 MiB | 277 MiB, 27 percent | 302 MiB, 29 percent | 302 MiB, 29 percent |
| compile worker | 1,024 MiB | 473 MiB, 46 percent | 525 MiB, 51 percent | 525 MiB, 51 percent |
| simulate worker | 512 MiB | 450 MiB, 88 percent | 466 MiB, 91 percent | 466 MiB, 91 percent |

- The wiring costs 4 to 40 MiB per pod: about 11 MiB on admission, 39 MiB on dispatch, 14 MiB on the workflow worker, 25 to 52 MiB on the workers.
- The simulate pod is above 80 percent at its first warm-up of the real workload component, before any staging-cap job (85 percent off, 88 percent on by `memory.peak`, about 80.5 percent by working set), so that line fails with the wiring off too. The staging-cap job on a warm pod peaks at 181 to 194 MiB over the warm pod.
- The workflow worker has no harness arm with the wiring off, so its 98 percent under the harness is not shown to come from the wiring. It is also 89 to 99 percent throttled at 500m while the harness runs.
- Smoke passes with the wiring on (10 of 10 checks, 43 s), and no service or worker was restarted or OOM killed in any arm.

## Storage growth

Bytes on each volume from the start of the first batch (`h-start`) to the end of the second (`harness-end`, 1.53 h later), with the data extrapolated to decision 20's retention.
The harness is the only load, so these are rates for 2,000-job batches on fake pools, with full trace sampling.

| Backend | At start | At end | Per hour | At retention | Proposed cap | Proposed volume |
| --- | --- | --- | --- | --- | --- | --- |
| Prometheus | 17.1 MB | 101.5 MB (285 MB after 3.4 h, mostly the write-ahead log) | about 11 MB for a compacted 2 h block of 21.9 MB at 2,490 to 2,610 samples/s (about 1.2 bytes per sample) | 240 h: about 2.6 GB, 3 GB with the log and head | 4 GB, as D20 | 5 GiB |
| Loki | 0.79 MB | 1.88 MB | about 1.7 MB (the peak between two snapshots) | 240 h: about 410 MB | none (Loki has no size key), so `WftObsVolumeFilling` | 2 GiB |
| Tempo | 0.92 MB | 13.7 MB, then flat while the pod was OOM looping | at least 16 MB | 72 h: at least 1.2 GB | 2 GB, not proven | 4 GiB |
| Pyroscope | 83.4 MB | 96.0 MB | about 8 MB | 72 h: about 0.6 GB | 1 GB | 2 GiB |
| Grafana | 1.6 MB | 251 MB | the plugin directory, then flat | not retention bound | none | 1 GiB |

Tempo's figure is a floor: the pod was killed repeatedly, so traces were dropped and the write-ahead log was replayed instead of flushed.
The 4 GiB Tempo volume of the throwaway stack holds both the floor and a 2.5 times margin, and S.1 and P.1 must confirm it on a stack that does not OOM.
Head series reached 77,300 to 85,700 and ingest is about 2,600 samples/s with 8 pods of the cluster plus the service and worker pods.

## Settled figures

D9's limits that held, and those that must change (requests equal limits):

| Component | D9 | Measured | Proposed | Reason |
| --- | --- | --- | --- | --- |
| Prometheus | 384 MiB, 200m | 321 MiB, 7 to 9m | 512 MiB, 100m | peak 84 percent of 384 MiB |
| Loki | 256 MiB, 100m | 106 MiB, 7m | 256 MiB, 100m | passes with room |
| Tempo | 256 MiB, 100m | OOM at 256, 384 and 512 MiB | at least 640 MiB, 200m, not proven | 83 percent of 512 MiB, 99 percent throttled, still killed |
| Pyroscope | 256 MiB, 100m | 152 MiB, 9m | 256 MiB, 100m | passes, and the first to drop |
| Grafana | 128 MiB, 50m | 260 MiB at 256 MiB | 384 MiB, 100m | 128 MiB is killed, 256 MiB is at 95 percent |
| collector | 128 MiB, 50m | 109 MiB, 3 to 5m | 160 MiB, 50m | 85 percent of 128 MiB by `memory.peak` |
| kube-state-metrics | 64 MiB, 20m | 24 MiB, 1m | 64 MiB, 20m | passes |
| state-inputs | 96 MiB, 20m | 43 MiB, 0m | 96 MiB, 50m | passes, 34 to 46 percent throttled at startup |

- The proposed sum is 2,368 MiB and 720m with Pyroscope, and 2,112 MiB without it, against 1,904 MiB and 1,400m allocatable on the 2,304 MiB node.
- R.2's `obs_stack`: CPU stays 1.5 (the mean CPU of the stack is under 100m, and the proposed requests are 720m). Memory needs a node of 2,816 MiB with Pyroscope or 2,560 MiB without it by the reservation rule (allocatable is the node less 400 MiB). This is the captain's decision. The `wft-obs` ResourceQuota (2,000 MiB) rises with it.
- The size caps and volumes are in the storage table. No time retention was lowered.
- D22's sample rate: the profiler's default rate costs 14 to 52 MiB per pod and was not the cause of any kill, so the worker pods do not need it lowered. The trace sampler is a different matter: Tempo is killed at full sampling, so D21's ratio of 1.0 needs lowering (a ratio of 0.1 is the first thing to try, and S13 did not test it).

## Findings

1. Tempo is the component that does not fit, and it fails because of trace volume rather than because of its configuration: it hits every limit up to 512 MiB under 2,000 jobs at a ratio of 1.0.
2. Grafana's startup plugin download and 3 minute start make the 128 MiB limit unworkable, and the startup must also be waited for by the readiness probe.
3. One `coredns` replica was Pending for Insufficient cpu once the stack was up, which shows how little CPU headroom the control plane node has with the switch on.
4. The simulate pod fails the 80 percent line before the wiring and before the staging-cap job, so the line needs a different baseline than `memory.peak`, or a larger simulate slot, whatever the stack does.
5. The workflow worker is CPU throttled at 500m under the harness. This is not the stack's cost but belongs to the load test's findings.

## Caveats

- The wiring is a throwaway stand-in. The H.3 helper may cost more or less, and its figures replace those here.
- `memory.peak` includes page cache, so the pod percentages are upper bounds. The working set polled every second gave 80.5 percent for the simulate pod's first run.
- Tempo's 512 MiB test ran with Pyroscope scaled to zero and only 700 jobs, and Tempo's ceiling at a lower sampling ratio was not measured.
- The harness runs fake pools, so it loads the control path and the workflow worker and not the guest workloads.
- One run of 3.4 hours is the longest window. Retention figures are extrapolations, and soak length belongs to #16.
- `budget.sh ci` could not count the obs figures because the obs modules are not in the repository yet.

## Reproduction

```
flock /tmp/wft-cluster.lock env WFT_CLUSTER_LOCK_HELD=1 WFT_OBS=on nix develop -c just up
nix develop -c docs/delivery/spikes/S13/code/apply.sh
nix develop -c python docs/delivery/spikes/S13/code/wire.py
nix develop -c docs/delivery/spikes/S13/code/arm.sh on <output directory>
nix develop -c docs/delivery/spikes/S13/code/harness-loop.sh 3600 <output directory>
nix develop -c python docs/delivery/spikes/S13/code/report.py <output directory>/snapshots.jsonl wft-obs
nix develop -c python docs/delivery/spikes/S13/code/queries.py 12300 wft-obs wft-control wft-workers
```
