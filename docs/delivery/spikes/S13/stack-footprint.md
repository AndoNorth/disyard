# Spike: footprint of the observability stack (S13)

Iteration 1 ran on 2026-11-21, and iteration 2 on 2026-11-22.
Owner: the observability epic, lane S.
Breakdown and method: [docs/delivery/epics/18-observability.md](../../epics/18-observability.md) and [docs/delivery/method.md](../../method.md).
Throwaway code: [code/](code/), applied with `code/apply.sh` (iteration 1 also used `code/wire.py`, which iteration 2 no longer needs).
Raw data (snapshots, polls, logs) stays outside the repository, and every figure below is read from it.

## Result

**Pass, after iteration 2, on a 4,096 MiB obs node.**
Iteration 1 was a Fail on the 2,304 MiB obs node of the `ci` preset, and its table is kept below.
Iteration 2 found a configuration that ran a clean 2-batch harness run (4,000 of 4,000 jobs finished, 0 failed) with no obs container restarted or OOM killed.

| Pass line part | Iteration 1 (2,304 MiB node) | Iteration 2 (4,096 MiB node, run 3) |
| --- | --- | --- |
| No obs container restarts or is OOM killed | Fail: Grafana at 128 MiB was OOM killed twice and exited once, and Tempo was OOM killed 31 times at 256 MiB, 5 times at 384 MiB and 2 times at 512 MiB | Pass: 0 restarts and 0 OOM kills in all 8 obs containers, Tempo at 1 GiB and trace ratio 1.0 |
| Every obs container peaks at most 90 percent of its limit | Fail: Grafana 100 percent of 128 MiB, Tempo 89 to 100 percent, Prometheus 84 percent at 384 MiB | Pass on the working set: the highest is Tempo at 73 percent of 1 GiB (750 MiB), then Prometheus at 62 percent. By `memory.peak`, which counts page cache, Prometheus reads 96 percent of 512 MiB and Tempo 85 percent |
| The sum of the limits fits the allocatable memory of the node | Fail: about 2,368 MiB against 1,904 MiB allocatable | Pass: 3,008 MiB against 3,584 MiB allocatable (84 percent), and 690m of CPU requests against 1,400m |
| Bytes per hour at retention fit a cap and volume | Storage fits | Storage fits, and Tempo's floor is replaced by a measured figure (see Storage growth) |

Two things limit how far the pass reaches, and the captain should read them before relying on it:

- Run 3 is the only clean 2-batch run, and it ran with Prometheus at 512 MiB.
  The settled figure for Prometheus is 640 MiB, because its `memory.peak` was 96 percent of 512 MiB, and no clean run confirmed 640 MiB: runs 2 and 4 hit a harness defect and run 5 was disturbed (see Iterations).
- Margins are thin under abnormal load.
  In run 5, with a storm of retried jobs, Tempo's working set reached 955 MiB (93 percent of 1 GiB), and a single wide LogQL query killed Loki at 256 MiB.

The five runs allowed by the brief are used.

## Question

Does the whole stack of D9, Tempo and Pyroscope included, run on the `ci` obs node at its `obs_stack` estimate, how fast does its storage grow, and what do the trace and profile wiring cost the service and worker pods, the simulate worker at its staging-cap peak included?
Iteration 2 asks the follow-up: which configuration of the stack runs under the 2,000-job harness with no container killed, on an obs node of at most 4,096 MiB, with Pyroscope kept and trace ratio 1.0 as the start?

## Method

### Iteration 1

- `WFT_OBS=on just up` on preset `ci`, under `/tmp/wft-cluster.lock`. The obs node is 1.5 CPU and 2,304 MiB.
- The eight obs Deployments of `code/stack.yaml` and `code/grafana.yaml`, at D9's limits, with requests equal to limits, strategy `Recreate`, and a scrape of every D1 target at 30 s.
- Prometheus keeps 10 days and 4 GB, Loki and Tempo and Pyroscope take the retention of decision 20.
- The wiring (`code/payload`, `code/wire.py`): an init container copies the OpenTelemetry SDK and `pyroscope-io` into an emptyDir, `PYTHONPATH` loads a `sitecustomize` that starts the tracer (sampler `ParentBased(TraceIdRatioBased(1.0))`), the OTLP log handler and the profiler, and a NetworkPolicy allows egress to the collector and Pyroscope. The services and workers are not changed, so the wiring is a stand-in for H.3.
- Arms: three smoke runs, a compile that misses the cache, and the staging-cap job (a 67,108,548 byte `io_churn` result, the peak `WORKER_RESERVE_MIB` is sized for), once with the wiring off and once on.
- Load: the workflow harness (`smoke --jobs 2000`, fake pools) for two batches (2,897 s and 2,595 s, 4,000 of 4,000 finished, 0 failed, 13 infra retries), with every panel query of every provisioned dashboard run once a minute (1,617 queries, 0 failed).
- Two more harness runs of 700 jobs each tested Tempo at 384 MiB and at 512 MiB (the second with Pyroscope scaled to zero to free node memory).
- Peaks: cgroup v2 `memory.peak`, `memory.current` less `inactive_file` polled every second, and cAdvisor through Prometheus (`max_over_time` of the working set over the load window). `memory.peak` includes page cache, so it is an upper bound on the working set.
- Volumes: `du -sb` of each local-path volume on the obs node.

### Iteration 2

- The same cluster path, with a local uncommitted override of `deploy/presets/ci.yaml` for the experiment only: `obs_stack.memory_mib` 4096 and `obs_memory_mib` 15488 (the 12,032 MiB of the preset less the 640 MiB obs node plus 4,096 MiB).
  The preset in the repository is unchanged, and R.2 owns those figures.
  The kubelet reports 3,584 MiB allocatable on the obs node, which is the reservation rule of iteration 1 (the node less 256 MiB less one sixteenth of the node).
- The host had at least 15 GiB of `MemAvailable` before the bring-up (15,377 MiB).
- The services are deployed by `scripts/tilt/run.sh ci`, and the same eight obs Deployments are applied by `code/apply.sh`.
  The services now carry the in-repository `wft_obs.setup` wiring (logs, traces and profiles, switched on by `OTEL_EXPORTER_OTLP_ENDPOINT` and `WFT_PYROSCOPE_ADDRESS`), so `code/wire.py` and its payload are not used, and the pod figures below are those of the shipped wiring.
- The pod NetworkPolicies of the repository select the collector, Pyroscope and Prometheus by the label `app.kubernetes.io/name`, so `code/stack.yaml` carries that label on those three pods.
- The state inputs stand-in (`code/exporter`) is built and pushed to the local registry, and its login is the `admission-logins` secret copied into `wft-obs` as `state-inputs-login`.
- Arms and load are those of iteration 1: the pod arm (`code/arm.sh`, then a compile that misses the cache) and the harness with a dashboard query load, through `code/harness-loop.sh`.
- Each run changed one thing, restarted every service and worker beforehand so the pod peaks count only that run, and was read from `memory.peak`, the cAdvisor working set and the pod restart and OOM counters.
- Peaks of obs containers are lifetime peaks of a container that was not restarted between runs (only the container whose limit changed starts again), so a later run includes the earlier ones.

## Iterations

All obs containers are 8; limits are memory limits with requests equal to limits.
Peaks are `memory.peak` at the end of the run (page cache included), with the cAdvisor working set over the run's window in the second table.

| Run | Change | Jobs | Obs restarts and OOM kills | Verdict |
| --- | --- | --- | --- | --- |
| 1 | Tempo 256 to 1,024 MiB (200m), ratio 1.0, node 4,096 MiB, Pyroscope on. Prometheus 512, Grafana 384 and collector 160 MiB as proposed in iteration 1 | 1 batch: 2,000 finished, 0 failed, 2 infra retries, 2,428 s | 0 and 0 | Tempo fits (64 percent). Grafana `memory.peak` is 101 percent of 384 MiB (its working set is 82 percent), so Grafana changes next |
| 2 | Grafana 384 to 512 MiB, then the pod arm | Invalid: batch 1 exited at 103 s, batch 2 finished 480 and failed 1,520 | 0 and 0 | Not a measurement of the stack: a harness defect, see below |
| 3 | None (the run 2 configuration again) | 2 batches: 4,000 finished, 0 failed, 4 infra retries, 2,184 s and 2,189 s | 0 and 0 | Pass on the working set. Prometheus `memory.peak` 96 percent of 512 MiB, so Prometheus changes next |
| 4 | Prometheus 512 to 640 MiB | Invalid: batch 1 exited at 164 s with the same defect | 0 and 0 | Not a measurement of the stack |
| 5 | The harness start made idempotent (`code/harness_idem.py`), same configuration as run 4 | Batch 1 only, stopped at 59 minutes: the jobs were failing, see below | 0 and 1 (Loki, see below) | Not a confirming run |

Peak by `memory.peak` at the end of the run (MiB, share of the limit in the run):

| Container | Limit (runs 1 / 2 and 3 / 5) | Run 1 | Run 2 | Run 3 | Run 5 |
| --- | --- | --- | --- | --- | --- |
| prometheus | 512 / 512 / 640 MiB | 193, 38 percent | 302, 59 percent | 493, 96 percent | 638, 100 percent |
| loki | 256 MiB | 108, 42 percent | 147, 57 percent | 147, 57 percent | 211, 82 percent (then killed, see below) |
| tempo | 1,024 MiB | 658, 64 percent | 869, 85 percent | 869, 85 percent (the peak of run 2) | 1,002, 98 percent |
| pyroscope | 256 MiB | 123, 48 percent | 150, 59 percent | 168, 66 percent | 169, 66 percent |
| grafana | 384 / 512 / 512 MiB | 386, 101 percent | 372, 73 percent | 372, 73 percent | 372, 73 percent |
| collector | 160 MiB | 71, 45 percent | 88, 55 percent | 88, 55 percent | 92, 57 percent |
| kube-state-metrics | 64 MiB | 23, 36 percent | 26, 40 percent | 27, 42 percent | 28, 43 percent |
| state-inputs | 96 MiB | 34, 35 percent | 34, 35 percent | 34, 35 percent | 34, 35 percent |

Peak working set over the run's window (cAdvisor `max_over_time`), the basis of the 90 percent line:

| Container | Run 1 (2,435 s) | Run 3 (4,384 s) | Run 5 (3,600 s) |
| --- | --- | --- | --- |
| prometheus | 167, 33 percent | 318, 62 percent | 438, 68 percent of 640 MiB |
| loki | 105, 41 percent | 112, 44 percent | not read (killed in the window) |
| tempo | 647, 63 percent | 750, 73 percent | 955, 93 percent |
| pyroscope | 99, 39 percent | 135, 53 percent | 133, 52 percent |
| grafana | 313, 82 percent of 384 MiB | 279, 55 percent | 277, 54 percent |
| collector | 70, 44 percent | 81, 51 percent | 86, 54 percent |
| kube-state-metrics | 22, 34 percent | 24, 38 percent | 24, 37 percent |
| state-inputs | 32, 34 percent | 32, 34 percent | 32, 34 percent |

Throttled share of CPU periods stayed low except for the stand-in state inputs: Tempo 1 to 2 percent at 200m, Prometheus 4 to 7 percent at 100m, Grafana 4 to 6 percent, collector 1 percent, Loki 0 percent, Pyroscope 3 to 4 percent, kube-state-metrics 8 to 10 percent, state-inputs 35 to 64 percent at 20m (its loop is idle most of the time).

### Run 1: Tempo at 1 GiB holds

Tempo received 56,037 spans for the 2,000 jobs (about 28 per job) and discarded none, the collector refused or failed none, and Tempo's working set peaked at 647 MiB, 63 percent of 1 GiB.
Every other container was under 50 percent of its limit except Grafana, whose first start reads 386 MiB by `memory.peak`: the plugin directory (249 MB) and its startup are page cache and download, and its working set over the load was 313 MiB.
Nothing restarted, so trace sampling, the Tempo tuning and the savings elsewhere (steps 2 to 4 of the plan) were not needed.

### Runs 2 and 4: a harness defect, not the stack

In both runs one `start_workflow` of the 2,000 was retried by the Temporal client after a timeout when its first attempt had already reached the server.
The retry raised `WorkflowAlreadyStartedError` (run 2, "already running") or an RPC error "already finished" (run 4), and `wft_workflow.harness` exited with status 1 after 103 s and 164 s.
The harness runs its fake pools in its own process, so the exit left the started jobs without pools.
In run 2 the next batch then ran against the orphans, and finished 480 jobs and failed 1,520 (3,688 s).
`start` in `packages/workflow/src/wft_workflow/harness/__init__.py` is not idempotent, and that file is outside this spike's scope, so it is reported here and not changed.
`code/harness_idem.py` wraps the harness and returns the existing handle when a start finds its workflow already there, and run 5 used it.
The same stack configuration passed run 3 cleanly, so the obs figures of runs 2 and 4 carry no verdict.

### Run 3: the clean pass

Two batches of 2,000 jobs (2,184 s and 2,189 s), 4,000 of 4,000 finished, 0 failed, 3 and 1 infra retries.
304,039 spans reached Tempo (cumulative since the cluster came up, including run 2) and none was discarded, 3,528 dashboard queries ran and none failed, head series reached 85,282 and ingest was about 2,730 samples/s.
No pod of `wft-obs`, `wft-control` or `wft-workers` restarted, and the sum of restarts over the 4,384 s window is 0.
All obs containers stay at or under 73 percent of their limits by working set.

### Run 5: why it is not a confirming run

Run 5 followed a clean-up of run 4: I terminated the 1,400 running workflows of the orphaned batches with the Temporal client, which leaves their job rows behind.
In the hour that followed:

- dispatch logged 32,921 `no intake queue intake.harness.standard` errors for tenant `harness` (about 135 log lines/s into Loki), which is consistent with its reconciler re-driving the rows of the terminated workflows.
  This is not proven;
- the workflow worker logged 1,207 failed and 1,696 succeeded job ends, and the failing sample ran its simulate tasks on the real `simulate-worker` (`guest_failed` after 0.1 to 0.2 s, 8 attempts), not on the harness fake pools;
- Tempo received 469,916 spans in total (165,877 more than at the end of run 3), and its working set peaked at 955 MiB, 93 percent of 1 GiB, with no kill and 0 discarded spans;
- Prometheus (640 MiB) had a `memory.peak` of 638 MiB and a working set of 438 MiB (68 percent), with head series at 93,097;
- `objectstore-0` (the infra node's `store` container, 384 MiB) was OOM killed once at 04:32, after about 12,000 harness jobs on the cluster, which is outside the obs stack;
- Loki (256 MiB) was OOM killed once at 05:14:22, seconds after I ran `{service_name=~".+"} | job_id="<id>"` over a 2 hour range by hand to find why jobs were failing.
  That is the query form of step 1 of "Following one job" in [deploy/obs/README.md](../../../../deploy/obs/README.md), so the documented query is enough to kill Loki at 256 MiB on a cluster with this much log volume.
  The harness load alone had taken Loki to 211 MiB (82 percent) by `memory.peak`.

I stopped run 5 after one batch, and it counts as the fifth run.

## Peak working set and throttled share (iteration 1)

Window: the 3.4 hours from the first harness batch to the end of the second, then idle.
The throttled share is `container_cpu_cfs_throttled_periods_total` over `container_cpu_cfs_periods_total`.
These are the iteration 1 figures at D9's limits, kept for the comparison; the settled limits are in the settled figures table.

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

## Service and worker pods, wiring on

Peak is `memory.peak` of the container, read from the node, so the percentages are upper bounds; the working set over the harness window is in the last column.
Every pod was restarted before the arm and before each harness run.
"Arm" is three smoke runs, the staging-cap job and a compile that misses the cache (run 2's arm).
"Harness" is run 3, two batches of 2,000 jobs on fake pools, which loads the control path and the workflow worker and not the guest workloads.

| Pod | Limit | Arm | Harness, 2 batches | Harness working set | Throttled in the harness |
| --- | --- | --- | --- | --- | --- |
| admission | 128 MiB | 90 MiB, 70 percent | 107 MiB, 83 percent | 103 MiB, 81 percent | 0 percent at 250m |
| dispatch | 256 MiB | 105 MiB, 41 percent | 119 MiB, 47 percent | 117 MiB, 46 percent | 1 percent at 500m |
| placement | 128 MiB | 59 MiB, 46 percent | 75 MiB, 58 percent | 70 MiB, 55 percent | 0 percent at 250m |
| workflow worker | 256 MiB | 107 MiB, 42 percent | 240 MiB, 94 percent | 236 MiB, 92 percent | 95 percent at 500m (mean 483m) |
| aggregate worker | 1,024 MiB | 310 MiB, 30 percent | not loaded (fake pools) | - | - |
| compile worker | 1,024 MiB | 570 MiB, 56 percent | not loaded (fake pools) | - | - |
| simulate worker | 512 MiB | 483 MiB, 94 percent | not loaded (fake pools) | - | - |

- With the shipped wiring the pods that sit near their limits are the workflow worker (94 percent by `memory.peak`, 92 percent by working set, and 95 percent CPU throttled at 500m while the harness runs), the simulate worker (94 percent after the staging-cap job and the cache-missing compile, 462 MiB before the compile) and admission (83 percent).
  Iteration 1 saw the workflow worker at 96 to 98 percent, the simulate pod at 88 to 91 percent and admission at 85 percent, so the shipped wiring is not worse than the stand-in.
- No arm was run with the stack off in iteration 2, so the share of the workflow worker's memory that belongs to the stack is not separated here.
- Smoke passed 10 of 10 checks in all three runs of the arm (54 s, 8 s and 7 s), and no service or worker was restarted or OOM killed.

### Proposed limit changes for these pods (not applied)

| Pod | Limit now | Proposed | Measurement behind it |
| --- | --- | --- | --- |
| workflow worker | 256 MiB, 500m | 384 MiB, 1,000m | 240 MiB peak is 63 percent of 384 MiB, and the pod used 483m of 500m while 95 percent of its CPU periods were throttled |
| simulate worker | 512 MiB | 640 MiB | 483 MiB peak is 75 percent of 640 MiB. The slot of the simulate pool node (896 MiB) and `WORKER_RESERVE_MIB` move with it, which belongs to R.2 and the slots lane |
| admission | 128 MiB | 160 MiB | 107 MiB peak is 67 percent of 160 MiB |

Dispatch (47 percent), placement (58 percent), the aggregate worker (30 percent) and the compile worker (56 percent) hold their limits.

## Storage growth

Bytes on each volume from the start of the run to its end, read by `du -sb` of each local-path volume on the obs node, for 2,000-job batches on fake pools with full trace sampling.

| Backend | Run 1 (0.68 h) | Run 3 (1.22 h) | At retention |
| --- | --- | --- | --- |
| Tempo | 0.1 to 15.7 MB, about 23 MB/h | 33.5 to 47.4 MB, about 11 MB/h | 72 h: about 0.8 to 1.7 GB, so the 2 GB cap and the 4 GiB volume hold |
| Prometheus | 3.5 to 33.2 MB | 119 to 237 MB (the write-ahead log and head, before the 2 h compaction) | iteration 1 measured about 11 MB/h compacted, 240 h about 2.6 GB, 3 GB with the log and head, cap 4 GB, volume 5 GiB |
| Loki | about 5 MB/h | flat, 9.9 to 8.0 MB (retention compaction) | iteration 1: 240 h about 410 MB, volume 2 GiB. A log flood (run 5, about 27 MB/h) shows the volume alarm is the only brake, since Loki has no size key |
| Pyroscope | 137 to 161 MB | 167 to 168 MB | iteration 1: about 8 MB/h, 72 h about 0.6 GB, cap 1 GB, volume 2 GiB |
| Grafana | 251 MB | 251 MB, flat | the plugin directory, then flat; volume 1 GiB |

Tempo's iteration 1 figure was a floor because the pod was killed; it is now measured on a stack that is not killed.
Head series reached 85,282 to 93,097 and ingest is 2,300 to 2,960 samples/s.
No time retention was changed.

## Settled figures

For R.2 (`obs_stack` of the `ci` preset) and the S and P lanes.
Requests equal limits.
Every row except two ran in run 3 at these figures: Prometheus ran at 512 MiB in run 3 and at 640 MiB only in the disturbed run 5, and state-inputs is the stand-in at its measured 20m.

| Component | Memory (requests = limits) | CPU (requests = limits) | Run 3 peak by working set | Reason |
| --- | --- | --- | --- | --- |
| Prometheus | 640 MiB | 100m | 318 MiB (62 percent of 512 MiB) | `memory.peak` read 96 percent of 512 MiB and 100 percent of 640 MiB because TSDB blocks fill page cache, and the working set was 68 percent of 640 MiB in run 5 |
| Loki | 256 MiB | 100m | 112 MiB (44 percent) | passes the harness; open item below for wide queries |
| Tempo | 1,024 MiB | 200m | 750 MiB (73 percent) | ratio 1.0 holds; 955 MiB (93 percent) under the retry storm of run 5 |
| Pyroscope | 256 MiB | 100m | 135 MiB (53 percent) | kept |
| Grafana | 512 MiB | 100m | 279 MiB (55 percent) | 384 MiB read 101 percent by `memory.peak` at its first start (82 percent by working set), 128 MiB is killed |
| collector | 160 MiB | 50m | 81 MiB (51 percent) | `memory_limiter` stays at 100 MiB |
| kube-state-metrics | 64 MiB | 20m | 24 MiB (38 percent) | passes |
| state-inputs (stand-in) | 96 MiB | 20m | 32 MiB (34 percent) | passes, 35 to 64 percent throttled with no effect |

- The sum is 3,008 MiB and 690m of CPU requests.
  The obs node is 4,096 MiB and 1.5 CPU, so allocatable is 3,584 MiB (the node less 256 MiB less one sixteenth) and 1,400m.
  The stack takes 84 percent of the memory and 49 percent of the CPU, which leaves 576 MiB.
- R.2's `obs_stack`: `memory_mib` 4096, `cpus` 1.5 (unchanged), and so `obs_memory_mib` 15488 (the 12,032 MiB of the preset less the 640 MiB obs node plus 4,096 MiB).
  The `wft-obs` ResourceQuota the kubelet and `namespaces.sh` rendered on this node is 3,764 MiB of requests and limits and 1,470m of CPU requests, which holds 3,008 MiB and 690m.
- Trace ratio stays 1.0.
  The sampler is `SAMPLE_RATIO = 1.0` in `packages/obs/src/wft_obs/traces.py`, the single key to lower if the span volume grows (the throwaway collector configuration has no sampling processor).
  Sampling at 0.5, 0.25 and 0.1 was not run because Tempo at 1 GiB held at 1.0.
- Pyroscope is kept.
  Dropping it was not needed, and it costs 135 MiB of working set and about 8 MB/h.
- The profile sample rate of D22 needs no change (iteration 1).
- The size caps and volumes are in the storage table.
- `scripts/cluster/budget.sh ci` was not re-run: the obs modules are still not in the repository, so it counts the obs node at 50 MiB, and the figures above are checked here by hand against the allocatable memory.

### Decisions left for the captain

1. Tempo's margin under abnormal load: 1,024 MiB held a clean run (73 percent) and a retry storm (93 percent, no kill).
   Raising it to 1,280 MiB costs 256 MiB (the sum becomes 3,264 MiB, 91 percent of allocatable), and trace ratio 0.5 halves the span volume at the cost of dropped traces.
2. Loki and the documented one-job query: `{service_name=~".+"} | job_id="<id>"` over 2 hours killed Loki at 256 MiB.
   Either raise Loki to 384 MiB (the sum becomes 3,136 MiB, 87 percent), which was not measured, or cap the query range with `limits_config.max_query_length` and fix the README query to a narrow range.
3. The limit changes proposed above for the workflow worker, the simulate worker and admission.
4. The `objectstore` `store` container (384 MiB) was OOM killed once after about 12,000 harness jobs on one cluster.
   It is outside the obs stack and not measured further.

## Findings

1. Tempo fits at 1 GiB and trace ratio 1.0 with the shipped wiring.
   Iteration 1's conclusion that D21's ratio must be lowered does not hold: the kills of iteration 1 were at 256 to 512 MiB with the throwaway wiring, and the shipped wiring sends about 28 spans per job, 56,037 spans for 2,000 jobs, with none discarded.
   The two wirings were not compared on span counts.
2. Grafana's startup plugin download and 3 minute start make the 128 MiB limit unworkable, and the startup must also be waited for by the readiness probe.
   At 384 MiB its `memory.peak` at first start is 101 percent (page cache), so 512 MiB is the settled figure.
3. `memory.peak` is not a usable 90 percent test for Prometheus: it reads 96 percent of 512 MiB and 100 percent of 640 MiB while the working set is 62 and 68 percent, because the TSDB blocks and the write-ahead log fill the page cache.
   The line should be read on the working set.
4. The harness start is not idempotent (see Runs 2 and 4).
   A start that the client retries ends the harness and strands its jobs.
   This is a flake of the load test, so it needs an owner outside this spike.
5. After a harness crash, terminating its workflows is not a clean-up: dispatch logged about 135 `no intake queue` error lines per second for the leftover rows, the failing jobs ran on the real pools, and the flood pushed Loki to 82 percent of its limit.
   A crashed run needs `just nuke`, and the next run needs a new cluster.
6. One `coredns` replica was Pending for Insufficient cpu once the stack was up, again in iteration 2, which shows how little CPU headroom the control plane node has with the switch on.
7. The simulate pod is above 80 percent at its first warm-up of the real workload component, whatever the stack does (iteration 1), and is at 94 percent after the cache-missing compile with the shipped wiring.
   Its slot needs a different baseline than `memory.peak`, or a larger limit (the proposal above).
8. The workflow worker is CPU throttled at 500m under the harness (95 percent of periods, mean 483m).
   This is not the stack's cost but belongs to the load test's findings.
9. The README row of S13 in `docs/delivery/spikes/README.md` still reads Fail, and it is outside the scope of this change.

## Caveats

- The settled Prometheus figure of 640 MiB is not proven by a clean 2-batch run.
  Run 3 passed at 512 MiB, and run 5 (disturbed) saw 640 MiB at 68 percent of its working set.
- Run 3 is the one clean 2-batch run, one run of 73 minutes, so the retention figures are extrapolations and soak length belongs to #16.
- Obs peaks are lifetime peaks of containers that were not restarted between runs, so run 3's Tempo `memory.peak` of 869 MiB comes from run 2, and its working set over its own window is 750 MiB.
- `memory.peak` includes page cache, so the pod percentages are upper bounds.
- The harness runs fake pools, so it loads the control path and the workflow worker and not the guest workloads, and the compile, simulate and aggregate pods were measured by the arm only.
- The state-inputs exporter is a stand-in, and the real one may cost more or less.
- The memory a user's Grafana or Loki query takes is not part of the load: the dashboard queries run at one hour ranges, and a 2 hour log scan killed Loki.

## Reproduction

```
free -m                                     # at least 15 GiB MemAvailable
# local preset override, not committed: in deploy/presets/ci.yaml set obs_stack.memory_mib 4096 and obs_memory_mib 15488
flock /tmp/wft-cluster.lock env WFT_CLUSTER_LOCK_HELD=1 WFT_OBS=on nix develop -c just up
WFT_CLUSTER_LOCK_HELD=1 nix develop -c scripts/tilt/run.sh ci
# state inputs stand-in: build docs/delivery/spikes/S13/code/exporter with BASE set to the pinned python image, push it
# to localhost:5001/s13-exporter:1, and copy the secret admission-logins of wft-control to wft-obs as state-inputs-login
nix develop -c docs/delivery/spikes/S13/code/apply.sh
nix develop -c docs/delivery/spikes/S13/code/arm.sh on <output directory>
nix develop -c docs/delivery/spikes/S13/code/harness-loop.sh 3600 <output directory>
nix develop -c python docs/delivery/spikes/S13/code/report.py <output directory>/snapshots.jsonl wft-obs
nix develop -c python docs/delivery/spikes/S13/code/queries.py 4384 wft-obs wft-control wft-workers
```

`harness-loop.sh` runs the harness through `code/harness_idem.py`, and a duration of 3,600 s runs two batches of about 2,200 s.
`queries.py` needs Prometheus forwarded on 127.0.0.1:19090 and `dashload.py` needs Grafana on 127.0.0.1:19300.
Iteration 1 additionally ran `code/wire.py` after `apply.sh`.
