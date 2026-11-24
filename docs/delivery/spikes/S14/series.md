# Spike: series and keys on the pinned versions (S14)

Run on 2026-11-21.
Owner: the [observability epic](https://github.com/AndoNorth/disyard/issues/13), lane A, filed as [spike issue 551](https://github.com/AndoNorth/disyard/issues/551).
Spec: [S14 in docs/delivery/epics/18-observability.md](../../epics/18-observability.md#4-issues), whose result gates A.1, A.2, the external list of C.1, the heartbeat line of E.4, P.1 and P.2.
Method: [docs/delivery/method.md](../../method.md).
Throwaway code: [code/](code/).
The cluster part runs as `flock /tmp/dsy-cluster.lock env DSY_CLUSTER_LOCK_HELD=1 nix develop -c docs/delivery/spikes/S14/code/run.sh <out-dir>`, which brings up the `ci` preset, deploys the `ci` profile, probes, and destroys the cluster.
The Tempo part runs in docker as `docs/delivery/spikes/S14/code/tempo-retention.sh worker` and `... scheduler`, and the Pyroscope, Loki, Prometheus and SeaweedFS checks as `nix develop -c docs/delivery/spikes/S14/code/backends.sh`.
The output below is trimmed to the lines each item names; `id`, `image` and `name` labels of cAdvisor lines are cut.

## Result

**Fail on item 6 only.**
Items 1 to 5 and 7 are present with a named label, item 5 through memory pressure in place of the `memory.events` `max` counter, and item 7 with no size cap in either backend.
Item 6 is absent: kubelet exports no volume stats for a local-path claim of the default type, and its fallback holds for Prometheus and Tempo but not for Loki, Pyroscope or the store as deployed.

| # | Pass-line item | Verdict | Series or key, exactly as exported |
| --- | --- | --- | --- |
| 1 | Temporal matching backlog count and age by queue | present, label `taskqueue` | `approximate_backlog_count`, `approximate_backlog_age_seconds` |
| 2 | Temporal heartbeat timeouts by queue | present, label `taskqueue` | `activity_task_timeout{timeout_type="Heartbeat"}` |
| 3 | SDK schedule-to-start latency and slots by queue | present, label `task_queue` | `temporal_activity_schedule_to_start_latency`, `temporal_worker_task_slots_available`, `temporal_worker_task_slots_used` |
| 4 | Last terminated exit code 137 of an OOM-killed container | present, once the container has restarted | `kube_pod_container_status_last_terminated_exitcode` |
| 5 | cAdvisor series that rises when a container hits its limit | present as memory pressure; the `memory.events` `max` counter itself is absent | `container_pressure_memory_stalled_seconds_total` |
| 6 | kubelet volume stats for a local-path claim | absent; the fallback is present for Prometheus and Tempo only | see [item 6](#6-kubelet-volume-stats-for-a-local-path-claim) |
| 7 | Time retention key in Tempo and Pyroscope, and a size cap | both have a time key, neither has a size cap | Tempo `backend_worker.compaction.block_retention`, Pyroscope `limits.retention_period` |

Three findings change what the lanes build, beyond the names:

- The Temporal server and the SDK spell the queue differently.
  The server's label is `taskqueue` and its value has every `-` written as `_` (`s14-pool` is exported as `s14_pool`, so `dsy-simulate` is `dsy_simulate`), while the SDK's label is `task_queue` with the name unchanged.
  A rule or panel that joins the two needs `label_replace`, and the `external` catalogue section records both spellings.
- Tempo 3 moved its retention key.
  Tempo 2's `compactor.compaction.block_retention` is gone (3.1.1 has no `-compactor.*` flag); in 3.1.1 the key is `backend_worker.compaction.block_retention`, the one the run shows deleting a block.
- The `memory.events` `max` signal of S10 lands on the pod cgroup, not the container cgroup, when one container's limit equals the pod's.
  Read from inside the container, as S10's `kubectl exec ... cat /sys/fs/cgroup/memory.events` does, it stays 0 while the pod cgroup's counter rises; this run reads 0 in the container and 2,347 at the pod.

## Question

Which of the series the rules need exist on Temporal server 1.32.2, the SDK of `uv.lock`, kube-state-metrics at its newest release, and the kind node image of `deploy/versions.yaml`, and which retention keys do Tempo and Pyroscope have at their newest releases?

## Environment

| Item | Value |
| --- | --- |
| Host | WSL2, kernel 6.6.87.2, 8 CPUs, 19 GiB, cgroup v2 |
| Cluster | the `ci` preset, kind node image v1.35.0 of `deploy/versions.yaml`, kubelet v1.35.0, containerd 2.2.0, node cgroup filesystem `cgroup2fs` |
| Temporal | the chart module of `deploy/temporal/`, which sets the `temporalio/server` 1.32.2 pin of `deploy/versions.yaml`, namespace `dsy`, default 4 partitions |
| SDK | `temporalio` 1.35.0 of `uv.lock`, the Python 3.13 of the dev shell, Prometheus endpoint from `PrometheusConfig` with its defaults |
| kube-state-metrics | v2.20.0 (released 2026-08-18, the newest), `registry.k8s.io/kube-state-metrics/kube-state-metrics@sha256:42cfe3723a5f058171c627537fb57a3ea0f26e4380fa18555a95cb1a1b4cfc5b` |
| Tempo | 3.1.1 (2026-10-21, the newest), `grafana/tempo@sha256:64e2fbd56d9c2b3412a4b430a58ea59e91a670b1e75f920a0c702213ad145044` |
| Pyroscope | 2.4.4 (2026-11-20, the newest), `grafana/pyroscope@sha256:288da0b9b73193b5aee0843b6f1f5d033ee697d4afa280efb95de338d1409870` |
| Loki, Prometheus | 3.7.10 and v3.16.0, the newest, for the storage series of item 6 only |
| SeaweedFS | the 4.51 pin of `deploy/versions.yaml` |

## Method

- [code/run.sh](code/run.sh) brings up the cluster and the `ci` profile, forwards the Temporal frontend and the matching and history pods' port 9090 to `127.0.0.1`, and runs [code/temporal_probe.py](code/temporal_probe.py).
- The probe runs one workflow worker and one activity worker on a pool-shaped queue `s14-pool` with `max_concurrent_activities=1`, as a pool worker runs, on the host with the SDK's Prometheus endpoint on.
  The deployed pool workers export no SDK metrics yet, which E.6 adds; the names and labels come from the SDK's core and are the same wherever the worker runs.
  Its workflow schedules 20 activities on `s14-backlog`, which nobody polls, runs 5 quick activities on `s14-pool`, and runs one activity with a 3 s heartbeat timeout that never heartbeats, with 2 attempts.
  After 75 s it prints every matching, history and SDK series line that names the queues.
- [code/obs-node.yaml](code/obs-node.yaml) runs kube-state-metrics, limited to pods, nodes and deployments, on the obs node in `dsy-obs`, with `s14-oom` (32 MiB limit, `tail /dev/zero`, restarted by the kubelet) and `s14-limit` (32 MiB limit, writing 128 MiB files in a loop, so its page cache meets the limit and is reclaimed without a kill).
  Neither is the node-wide memory canary of `scripts/cluster/canary.sh`.
- [code/volumes.yaml](code/volumes.yaml) binds two 64 MiB claims of the `standard` local-path class on the obs node, one of the provisioner's default type and one annotated `volumeType: local`, and writes 8 MiB to each.
- After 120 s the run scrapes kube-state-metrics, the obs node's cAdvisor and kubelet endpoints through the API server, and reads `memory.events` of `s14-limit`'s pod and container cgroups on the node.
- [code/tempo-retention.sh](code/tempo-retention.sh) runs Tempo monolithic on local storage with one of its two `block_retention` keys at 2 minutes, pushes one trace, and watches Tempo's own blocklist series and log until the block is deleted.

## Evidence

### Cluster run, `run.sh`, rc 0

```text
== versions
kubelet v1.35.0, containerd://2.2.0
node cgroup filesystem cgroup2fs
temporalio 1.35.0
kube-state-metrics registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.20.0@sha256:42cfe3723a5f058171c627537fb57a3ea0f26e4380fa18555a95cb1a1b4cfc5b
== matching http://127.0.0.1:19024/metrics: 16 lines matching 's14-backlog'
approximate_backlog_age_seconds{namespace="dsy",namespace_state="active",operation="TaskQueuePartitionManager",partition="0",service_name="matching",task_type="Activity",taskqueue="s14_backlog",type="matching",worker_build_id="",worker_deployment_name="",worker_version="__unversioned__"} 55.887505257
approximate_backlog_count{namespace="dsy",namespace_state="active",operation="TaskQueuePartitionManager",partition="0",service_name="matching",task_priority="3",task_type="Activity",taskqueue="s14_backlog",type="matching",worker_build_id="",worker_deployment_name="",worker_version="__unversioned__"} 5
approximate_backlog_count{...,partition="1",...,task_type="Activity",taskqueue="s14_backlog",...} 4
approximate_backlog_count{...,partition="2",...,task_type="Activity",taskqueue="s14_backlog",...} 7
approximate_backlog_count{...,partition="3",...,task_type="Activity",taskqueue="s14_backlog",...} 4
== history http://127.0.0.1:19025/metrics: 5 lines matching ''
activity_task_timeout{activityType="silent",namespace="dsy",operation="TimerActiveTaskActivityTimeout",service_name="history",taskqueue="s14_pool",timeout_type="Heartbeat",type="history",versioning_behavior="Unspecified",workflowType="Probe"} 2
activity_timeout{activityType="silent",namespace="dsy",operation="TimerActiveTaskActivityTimeout",service_name="history",taskqueue="s14_pool",timeout_type="Heartbeat",type="history",versioning_behavior="Unspecified",workflowType="Probe"} 1
heartbeat_timeout{first_attempt="false",namespace="dsy",operation="TimerActiveTaskActivityTimeout",service_name="history",type="history",versioning_behavior="Unspecified"} 1
== sdk http://127.0.0.1:19026/metrics: 13 lines matching 's14-pool'
temporal_activity_schedule_to_start_latency_bucket{namespace="dsy",service_name="temporal-core-sdk",task_queue="s14-pool",le="100"} 6
temporal_activity_schedule_to_start_latency_bucket{namespace="dsy",service_name="temporal-core-sdk",task_queue="s14-pool",le="5000"} 7
temporal_activity_schedule_to_start_latency_sum{namespace="dsy",service_name="temporal-core-sdk",task_queue="s14-pool"} 3269
temporal_activity_schedule_to_start_latency_count{namespace="dsy",service_name="temporal-core-sdk",task_queue="s14-pool"} 7
temporal_num_pollers{namespace="dsy",poller_type="activity_task",service_name="temporal-core-sdk",task_queue="s14-pool"} 1
temporal_worker_task_slots_available{namespace="dsy",service_name="temporal-core-sdk",task_queue="s14-pool",worker_type="ActivityWorker"} 1
temporal_worker_task_slots_used{namespace="dsy",service_name="temporal-core-sdk",task_queue="s14-pool",worker_type="ActivityWorker"} 0
NAME                       READY   STATUS      RESTARTS      AGE     IP           NODE
s14-ksm-6d9958b687-8kc86   1/1     Running     0             2m15s   10.244.2.2   dsy-worker2
s14-limit                  1/1     Running     0             2m15s   10.244.2.4   dsy-worker2
s14-oom                    0/1     OOMKilled   3 (83s ago)   2m15s   10.244.2.3   dsy-worker2
s14-volumes                1/1     Running     0             2m14s   10.244.2.7   dsy-worker2
== kube-state-metrics
kube_pod_container_status_last_terminated_reason{namespace="dsy-obs",pod="s14-oom",uid="408e7c0c-ee20-4e9b-a5ad-afae1e807b35",container="oom",reason="OOMKilled"} 1
kube_pod_container_status_last_terminated_exitcode{namespace="dsy-obs",pod="s14-oom",uid="408e7c0c-ee20-4e9b-a5ad-afae1e807b35",container="oom"} 137
kube_pod_container_status_restarts_total{namespace="dsy-obs",pod="s14-oom",uid="408e7c0c-ee20-4e9b-a5ad-afae1e807b35",container="oom"} 3
== cadvisor on dsy-worker2
container_memory_failcnt{container="",namespace="dsy-obs",pod="s14-limit"} 0 1795273128939
container_memory_failcnt{container="limit",namespace="dsy-obs",pod="s14-limit"} 0 1795273116435
container_oom_events_total{container="",namespace="dsy-obs",pod="s14-limit"} 0 1795273128939
container_oom_events_total{container="limit",namespace="dsy-obs",pod="s14-limit"} 0 1795273116435
container_pressure_memory_stalled_seconds_total{container="",namespace="dsy-obs",pod="s14-limit"} 13.56077 1795273128939
container_pressure_memory_stalled_seconds_total{container="limit",namespace="dsy-obs",pod="s14-limit"} 12.050016 1795273116435
== memory.events of s14-limit, pod cgroup and container cgroups, read on the node
pod max 2347 oom_kill 0
cri-containerd-396516c973d7507732e48af8d8147412b4d0e01d7bba6191200dac4cb6426fe0.scope max 0 oom_kill 0
cri-containerd-9d0d32c250244c1253ab48827651ce18206aa1da00d2ac1669de529f519e0536.scope max 0 oom_kill 0
== memory pressure on every node: containers above 0.01 s stalled, other than s14-limit
dsy-worker3 container_pressure_memory_stalled_seconds_total{container="kindnet-cni",namespace="kube-system",pod="kindnet-c2qgb"} 0.017905
dsy-worker5 container_pressure_memory_stalled_seconds_total{container="kube-proxy",namespace="kube-system",pod="kube-proxy-k2mdr"} 0.019791
end of list
== kubelet volume stats on dsy-worker2
CLAIM                HOSTPATH                                                                                    LOCAL
s14-local            <none>                                                                                      /var/local-path-provisioner/pvc-96ed3b50-8baf-44c8-bcfd-14fab4b06ad9_dsy-obs_s14-local
s14-hostpath         /var/local-path-provisioner/pvc-a5f4aa66-8c9e-4d46-bacc-42e343459c37_dsy-obs_s14-hostpath   <none>
kubelet_volume_stats_capacity_bytes{namespace="dsy-obs",persistentvolumeclaim="s14-local"} 1.081101176832e+12
kubelet_volume_stats_used_bytes{namespace="dsy-obs",persistentvolumeclaim="s14-local"} 2.53540818944e+11
8196	/hostpath
8196	/local
```

### The store's metrics port on the same cluster

Scraped from `objectstore-0` port 9327 on an earlier bring-up of the same `ci` profile, through `kubectl port-forward pod/objectstore-0`:

```text
store:  ["server","-s3","-s3.config=/etc/seaweedfs/s3.json","-dir=/data","-volume.max=0","-master.volumeSizeLimitMB=292"]
worker: ["sh","-c","... exec /usr/bin/weed -logtostderr=true worker -admin=127.0.0.1:12535 -jobType=s3_lifecycle -workingDir=/worker -metricsPort=9327"]
SeaweedFS_volumeServer_* names served on 9327: concurrent_download_limit concurrent_upload_limit file_read_failures
  file_read_invalid_needles file_write_failures in_flight_download_size in_flight_upload_size max_volumes
  replication_targets start_time_seconds storage_io_error_total
```

### Backends, `backends.sh`, rc 0

```text
== grafana/pyroscope:2.4.4 [-retention-period=2m -ingester.min-ready-duration=0]
139:        cleanup_grace_period: 6h0m0s
140:        cleanup_interval: 15m0s
445:    compactor_blocks_retention_period: 0s
457:    write_path: segment-writer
466:    retention_period: 2m
635:    min_free_disk_gb: 10
636:    min_disk_available_percentage: 0.05
803:architecture_storage: v1-v2-dual
# TYPE pyroscope_distributor_inflight_bytes_limit gauge
# TYPE pyroscope_memberlist_client_messages_in_broadcast_queue_bytes gauge
# TYPE pyroscope_metastore_boltdb_persist_snapshot_size_bytes gauge
# HELP objstore_bucket_operation_transferred_bytes Number of bytes transferred from/to bucket per operation.
# TYPE objstore_bucket_operation_transferred_bytes histogram
# HELP pyroscope_segment_writer_client_sent_bytes Number of bytes sent by the segment writer client.
# TYPE pyroscope_segment_writer_client_sent_bytes histogram
== prom/prometheus:v3.16.0 [--config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/prometheus]
# HELP prometheus_tsdb_head_chunks_storage_size_bytes Size of the chunks_head directory.
# TYPE prometheus_tsdb_head_chunks_storage_size_bytes gauge
# HELP prometheus_tsdb_storage_blocks_bytes The number of bytes that are currently used for local storage by all blocks.
# TYPE prometheus_tsdb_storage_blocks_bytes gauge
# HELP prometheus_tsdb_wal_storage_size_bytes Size of the write log directory.
# TYPE prometheus_tsdb_wal_storage_size_bytes gauge
== grafana/loki:3.7.10 [-config.file=/etc/loki/local-config.yaml]
# TYPE loki_distributor_inflight_bytes gauge
# TYPE loki_embeddedcache_memory_bytes gauge
# TYPE loki_ingester_memory_streams_labels_bytes gauge
# TYPE loki_ingester_wal_bytes_in_use gauge
# TYPE loki_rate_store_max_stream_rate_bytes gauge
# TYPE loki_rate_store_max_unique_stream_rate_bytes gauge
# HELP loki_ingester_chunk_size_bytes Distribution of stored chunk sizes (when stored).
# TYPE loki_ingester_chunk_size_bytes histogram
== chrislusf/seaweedfs:4.51@sha256:f7a49e3f3972ebed5be2ed4befa4a0c68eb7bdddba6e692d42729ceb545134b8 [server -dir=/data -volume.max=0 -master.volumeSizeLimitMB=292 -metricsPort=9328]
port 9333 /metrics 404
port 8080 /metrics 400
{"name":"f","size":4194304,"eTag":"63b869a7"}
SeaweedFS_volumeServer_resource{name="/data",type="all"} 1.081101176832e+12
SeaweedFS_volumeServer_resource{name="/data",type="avail"} 7.70038418432e+11
SeaweedFS_volumeServer_resource{name="/data",type="free"} 7.80849430528e+11
SeaweedFS_volumeServer_resource{name="/data",type="used"} 3.00251746304e+11
SeaweedFS_volumeServer_total_disk_size{collection="",type="deleted_bytes"} 0
SeaweedFS_volumeServer_total_disk_size{collection="",type="normal"} 4.194408e+06
```

### Tempo, `tempo-retention.sh`

```text
[15:02:40] image sha256:d1b47a86637121283f8bb3d9342ee2c77bd2bb92b1cb2dfb35f2b13629498961 key scheduler
[15:02:40] pushed trace 2cd18858b908b98d05e406fd7918276b
[15:03:39] block in backend after 65 s: blocklist 1, live bytes 21724, trace lookup 200
[15:22:27] block not gone within 1200 s: blocklist 1, live bytes 21724, trace lookup 200
[15:02:40] image sha256:d1b47a86637121283f8bb3d9342ee2c77bd2bb92b1cb2dfb35f2b13629498961 key worker
[15:02:40] pushed trace e8d9532de1c5b11bb5b4436084a79493
[15:03:39] block in backend after 65 s: blocklist 1, live bytes 21724, trace lookup 200
[15:05:07] block gone after 155 s: blocklist 0, live bytes 0, trace lookup 200
level=info ts=2026-11-21T15:04:57.550817982Z caller=retention.go:91 msg="marking block for deletion" blockID=dbb83971-82d8-4313-8fc5-fa0d889bcfe1 tenantID=singl
```

## Findings per item

### 1. Temporal matching backlog

Present on the matching service's port 9090.

- `approximate_backlog_count{namespace,partition,task_type,task_priority,taskqueue,worker_version,...}` and `approximate_backlog_age_seconds{...}` with the same labels except `task_priority`.
- One series per partition and task type: the default 4 partitions split the 20 backlogged activities 5, 4, 7 and 4, each with an age of 56 to 59 s.
- A rule reads `sum by (taskqueue) (approximate_backlog_count{task_type="Activity"})` and `max by (taskqueue) (approximate_backlog_age_seconds{task_type="Activity"})`.
- Every series also carries `namespace_state`, `operation="TaskQueuePartitionManager"`, `service_name`, `type`, `worker_build_id` and `worker_deployment_name`, all bounded.

### 2. Temporal heartbeat timeouts

Present on the history service's port 9090, so E.4's workflow counter is not needed.

- `activity_task_timeout{timeout_type="Heartbeat",taskqueue,activityType,workflowType,namespace,...}` counts each attempt that timed out: 2 for the probe's 2 attempts.
- `activity_timeout{...}` with the same labels counts the activity once, when its last attempt times out: 1.
- `heartbeat_timeout{first_attempt,namespace,...}` has no queue label, so the rules do not use it.
- A rule reads `sum by (taskqueue) (increase(activity_task_timeout{timeout_type="Heartbeat"}[15m]))`.

### 3. SDK schedule-to-start latency and slots

Present on the worker's SDK endpoint.

- `temporal_activity_schedule_to_start_latency_bucket{namespace,task_queue,service_name,le}`, with `_sum` and `_count`, is a histogram in milliseconds under the SDK's default `PrometheusConfig`: the buckets are `le="100"` to `le="1000000"` and the name has no `_seconds` suffix.
- `temporal_worker_task_slots_available{task_queue,worker_type="ActivityWorker",...}` reads 1 and `temporal_worker_task_slots_used` reads 0 on the idle single-slot worker.
- `temporal_num_pollers{poller_type="activity_task",task_queue}` is also exported.

### 4. Last terminated exit code

Present in kube-state-metrics v2.20.0 once the container has restarted.

- `kube_pod_container_status_last_terminated_exitcode{namespace,pod,uid,container}` reads 137 and `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` reads 1 after the kubelet restarts `s14-oom`.
- A first try with `restartPolicy: Never` exported only `kube_pod_container_status_terminated_reason{reason="OOMKilled"}` and no exit code series, because a container that never restarts has no last termination.
  The exit test row 8 pod keeps the default `restartPolicy: Always`, as [code/obs-node.yaml](code/obs-node.yaml) does.

### 5. Container at its limit

The `memory.events` `max` counter is absent from cAdvisor, and memory pressure is the series that rises.

- `container_memory_failcnt` exists and reads 0 at the container and pod level while the pod cgroup's `memory.events` `max` is 2,347, so cAdvisor does not fill it from cgroup v2's `memory.events`.
- `container_oom_events_total` exists and stays 0 for a reclaimed container, so it does not show a container held at its limit.
- `container_pressure_memory_stalled_seconds_total{namespace,pod,container}` (and `..._waiting_seconds_total`) rises to 12.05 s for `s14-limit` (13.56 s at its pod) within 2 minutes, while every other container on the five nodes stays at or below 0.02 s since the cluster started.
- Memory pressure also rises when the node itself is short of memory, which is worth an alert too, so the alert keeps its name and reads pressure: A.2 sets the threshold on `rate(container_pressure_memory_stalled_seconds_total{container!=""}[5m])`, and the run's figures suggest 0.05, 5 percent of wall time.
- S10's figure of no `max` rise, read inside each Temporal container, does not prove the pods never met their limit, by the pod cgroup finding above.

### 6. kubelet volume stats for a local-path claim

Absent.

- The provisioner's default claim is a `hostPath` volume, and kubelet exports no `kubelet_volume_stats_*` series for it.
  This holds for every claim the cluster has today, such as `data-objectstore-0`.
- A claim annotated `volumeType: local` gets the series, but they are `statfs` of the filesystem under the node, the host disk: capacity 1,081,101,176,832 bytes and used 253,540,818,944 bytes for a claim holding 8 MiB.
  They say how full the host disk is, not the claim.

Fallback, each backend's self-reported storage bytes:

| Backend | Series | State |
| --- | --- | --- |
| Prometheus v3.16.0 | `prometheus_tsdb_storage_blocks_bytes` + `prometheus_tsdb_wal_storage_size_bytes` + `prometheus_tsdb_head_chunks_storage_size_bytes` | present, gauges |
| Tempo 3.1.1 | `tempodb_backend_bytes_total{status="live",tenant}` | present, a gauge despite its `_total` name |
| Loki 3.7.10 | none; `loki_ingester_chunk_size_bytes` is a histogram of the chunks written | absent |
| Pyroscope 2.4.4 | none; `objstore_bucket_operation_transferred_bytes` and `pyroscope_segment_writer_client_sent_bytes` are histograms of bytes moved | absent |
| SeaweedFS 4.51 on 9327 | none: 9327 is the lifecycle worker's endpoint (`weed worker -metricsPort=9327`), which exports no disk series | absent |

- SeaweedFS exports `SeaweedFS_volumeServer_total_disk_size{collection,type="normal"}`, the bytes its volumes hold (4,194,408 after a 4 MiB write), only from `weed server -metricsPort=<port>`, which the store container does not set; without the flag no store port serves `/metrics`.
  `SeaweedFS_volumeServer_resource{name="/data",type}` is `statfs` of the host disk again.
- So `DsyObsVolumeFilling` can read Prometheus and Tempo, and `DsyStoreCapacityHigh` needs `-metricsPort` on the store's `weed server`, a change to the closed object store epic's module.

### 7. Tempo and Pyroscope retention

Tempo 3.1.1 has two `block_retention` keys, both default 336h:

- `backend_worker.compaction.block_retention` (flag `-backend-worker.compaction.block-retention`) enforces retention: set to 2m, the block left the blocklist 155 s after the trace was pushed, 90 s after it reached the backend, with `tempodb_backend_bytes_total{status="live"}` back to 0.
- `backend_scheduler.provider.compaction.compaction.block_retention` alone deletes nothing: set to 2m with the worker key at 336h, the block was still listed 1,200 s after the push, so it is not the key to set.
- The trace lookup kept answering 200 after the block was deleted, from the recent data the live-store holds; this does not matter at a 3-day retention.
- Retention runs every `backend_scheduler.provider.retention.interval` (1h by default) and removes a marked block after `compacted_block_retention` (1h).
- No size cap: `max_block_bytes` bounds one block, and no key bounds the bytes held.
- Monolithic mode on local storage runs without Kafka, with the backend scheduler and worker in the one process.

Pyroscope 2.4.4 runs storage `v1-v2-dual` with write path `segment-writer` by default, so new data goes to the v2 storage:

- `limits.retention_period` (flag `-retention-period`, default `31d`) is the v2 time retention; the run sets `-retention-period=2m` and the effective config shows `retention_period: 2m`.
- v2 deletes by the metastore index cleaner every `metastore.index.cleanup_interval` (15m), on whole index partitions, after `metastore.index.cleanup_grace_period` (6h); the deletion is not observed here, because it needs hours past the grace period.
- `limits.compactor_blocks_retention_period` and `pyroscopedb.min_free_disk_gb` with `min_disk_available_percentage` are marked `[v1 storage only]`; the second pair keeps free space on the filesystem, which under local-path is the host disk, so it is not a size cap either.
- No size cap for v2.

## Decision

- **Catalogue `external` (C.1)** takes the names of the table above, with the server's `taskqueue` label and `_` spelling and the SDK's `task_queue` label and `-` spelling both recorded.
- **Heartbeat line of E.4**: no workflow counter; A.2 reads `activity_task_timeout{timeout_type="Heartbeat"}` by `taskqueue`.
- **OOM (A.1, exit test row 8)**: `kube_pod_container_status_last_terminated_exitcode == 137` from kube-state-metrics v2.20.0, and the row 8 pod restarts, as [code/obs-node.yaml](code/obs-node.yaml) does.
- **Container at its limit (A.2)**: the alert is kept on `container_pressure_memory_stalled_seconds_total`, not dropped, so #6 gains no leftover; A.2 picks the threshold near 0.05 of wall time from the figures above.
- **`DsyObsVolumeFilling` (A.2, S.1, S.2, P.1, P.2)**: Prometheus reads the sum of its three `prometheus_tsdb_*_bytes` gauges and Tempo `tempodb_backend_bytes_total{status="live"}`.
  Loki and Pyroscope report no stored bytes, so the fallback is incomplete for them; the proposal is that Loki's rule reads `increase(loki_ingester_chunk_size_bytes_sum[10d])`, the chunk bytes written within its 10-day retention, as an estimate that leaves out the index, and that Pyroscope has no volume alert and keeps S13's volume size with its margin.
  Loki's 8 GB of decision 20 is enforced only by this alert, so the estimate is a question for the captain in the pull request.
- **`DsyStoreCapacityHigh`**: port 9327 is the lifecycle worker's and has no disk series, so the rule needs `-metricsPort` on the store's `weed server`, on a port of its own, and reads `SeaweedFS_volumeServer_total_disk_size{type="normal"}`; that flag is a change to `deploy/objectstore/`, a `revisit` child of its closed epic, and until it lands the alert has no input.
- **D24 (P.1, P.2)**: Tempo sets `backend_worker.compaction.block_retention: 72h` and Pyroscope `limits.retention_period: 3d`, the times of decision 20; neither has a size key, so both size caps are D24's alert-only cap, which for Pyroscope has no series as above.

## Departures

- Item 3 is read from a stand-in single-slot worker on the host, not the deployed pool worker, which has no SDK metrics endpoint until E.6.
- Item 7 runs Tempo and Pyroscope in docker on the host, not on the obs node, because their modules are lane S's and the keys do not depend on where the binary runs.
- Pyroscope's v2 deletion is read from its flags and config, not observed.
