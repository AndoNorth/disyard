# Spike: pool queue order (S12)

Run on 2026-11-18.
Owner: the [task placement scheduler epic](https://github.com/AndoNorth/disyard/issues/9), lane S, filed as [spike issue 474](https://github.com/AndoNorth/disyard/issues/474).
Spec: [S12 in docs/delivery/epics/14-placement.md](../../epics/14-placement.md#s12-pool-queue-order), whose result informs D14, #15 and #16 and gates nothing.
Method: [docs/delivery/method.md](../../method.md).
Throwaway code: [code/](code/), run as `nix develop -c docs/delivery/spikes/S12/code/run.sh --activities 200 --gaps-ms 20,0 --repeat 3`, then the control with `--partitions 1` added.
Every number below is a `figure <name> <value>` line of those runs, named `run<n>_gap<ms>.<figure>` in each of the two runs' output.

## Result

**Fail.**
With the server's default of 4 partitions, every one of 200 activities starts, all within 1.6 s of the worker starting, but the order breaks the line of 50 in 4 of 6 runs.
Activities scheduled one per workflow task, a timer apart, pass in 2 of 3 runs and miss by one in the third (largest overtaking 35, 40 and 51).
Activities scheduled together in one workflow task, as a job's sibling tasks are, fail in 3 of 3 with a largest overtaking of 146 to 157, close to a random order.

The control run at one partition separates two causes.
Spaced activities start in exactly the order they were scheduled, 0 inversions in 3 of 3 runs, so the partitions alone make the overtaking across workflow tasks.
Batched activities still fail at one partition, with a largest overtaking of 68 to 125, so the order inside one workflow task is lost before matching, where the server dispatches the 200 activity tasks of one workflow task, and one partition does not restore it.
Starvation is not seen: every activity of every run started, the slowest run within 1.6 s.

**D14 is decided as option (b):** the pool queues keep the server's default of 4 partitions, and start order across jobs on a pool queue is approximate, as recorded in D14 of the epic.
One partition through #6's Temporal module stays the switch: it gives first-in-first-out order between tasks scheduled in different workflow tasks, as tasks of different jobs and different layers of one job are, and leaves the order among the sibling tasks of one workflow task unordered.

## Question

With the server's default partitions, in what order does one single-slot worker start activities scheduled one after another on one pool-shaped queue, and does every one start?

## Departure from the spec block

The block names tier T2, the `ci` cluster's Temporal through a `kubectl port-forward`.
This run is at T1, one Temporal dev server through `dsy_itest`, because the run was dispatched with no cluster.
What the departure keeps: the `ci` chart sets no partition key (`deploy/temporal/values.json` holds only the two cache keys), so it runs the server's default, and the dev server answered `ListTaskQueuePartitions` with 4 activity partitions on every fresh queue (`run<n>.activity_partitions`), the server's default.
What it leaves unmeasured: the chart's matching service on Postgres persistence with 512 history shards, where the dev server runs every service in one process on SQLite, and server 1.32.2 of the chart against the CLI's embedded 1.32.0.
The partition count is the mechanism the question and D14 are about, and it is the same on both, so the fail is expected to hold on `ci`; a T2 rerun is the code above with `main()` connecting to a port-forward address in place of starting the container.

## Environment

| Item | Value |
| --- | --- |
| Host | WSL2, kernel 6.6.87.2 (`host.kernel`), 8 CPUs (`host.cpus`) |
| Other work on the host | a RabbitMQ container of another worker ran throughout, and a paired T1 session of another worker held both lock files before the control run; neither was touched |
| Temporal | the `temporalio/temporal` 1.9.1 pin of `deploy/versions.yaml`, `server start-dev --headless`, server 1.32.0 (`temporal.server_version`), started by `dsy_itest.Session(containers=1)` at its 1 GiB memory cap |
| Client | Python 3.13.15 of the dev shell (`host.python`), `temporalio` 1.35.0 as locked in `uv.lock` |

## Method

- [code/measure.py](code/measure.py) starts one dev server through `dsy_itest` and runs each measurement on a fresh pair of queues, `s12-<hex>-control` for the measurement workflow and `s12-<hex>-dsy-simulate` for the activities, so no run sees another's backlog and no contract queue is touched.
- The measurement workflow schedules activities numbered 0 to 199 on the pool-shaped queue, which has no worker.
  - Gap 20 ms: each activity in its own workflow task, followed by a 20 ms timer, so the server receives them one after another; the dev server fires each timer about 1 s later, so the 200 are scheduled over 200 s (`schedule_s`).
  - Gap 0: all 200 in one workflow task, as a job's sibling tasks are scheduled in one task (`schedule_s` 0.03 to 0.07).
- The workflow worker runs with eager activity execution off, so every activity goes through matching.
- Once a describe of the workflow shows 200 pending activities, one worker with `max_concurrent_activities=1` starts on the pool-shaped queue and records the number of each activity it starts, in order; each activity returns at once.
- The number is the schedule order: the overtaking of activity `i` is the count of activities numbered above `i` that started before it, the largest overtaking is the maximum over the run, and the inversion count is the sum, out of 19,900 possible pairs.
- The pass line of the block: 200 of 200 start within 10 minutes, and the largest overtaking is at most 50.
- The control run adds `--dynamic-config-value matching.numTaskqueueReadPartitions=1` and the same for the write partitions to the dev server, and is otherwise the same.

## Measured numbers

### Default partitions (4)

| Run | Gap | Partitions | Started | All started in | Inversions | Largest overtaking | Activity overtaken most, started at position | Pass |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 20 ms | 4 | 200 | 1.59 s | 2,317 | **51** | 86, at 137 | no |
| 3 | 20 ms | 4 | 200 | 1.24 s | 1,408 | 40 | 141, at 181 | yes |
| 5 | 20 ms | 4 | 200 | 1.06 s | 1,945 | 35 | 101, at 136 | yes |
| 2 | 0 | 4 | 200 | 0.66 s | 6,463 | **155** | 12, at 167 | no |
| 4 | 0 | 4 | 200 | 1.09 s | 6,946 | **157** | 1, at 158 | no |
| 6 | 0 | 4 | 200 | 0.65 s | 7,454 | **146** | 31, at 177 | no |

No activity was retried (`retried` 0 in every run).
A random order of 200 has about 9,950 inversions; the batch runs are at 65 to 75 percent of that.

### Control, one partition

| Run | Gap | Partitions | Started | All started in | Inversions | Largest overtaking | Activity overtaken most, started at position | Pass |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 20 ms | 1 | 200 | 0.68 s | 0 | 0 | none | yes |
| 3 | 20 ms | 1 | 200 | 0.67 s | 0 | 0 | none | yes |
| 5 | 20 ms | 1 | 200 | 0.72 s | 0 | 0 | none | yes |
| 2 | 0 | 1 | 200 | 0.64 s | 5,355 | **85** | 11, at 96 | no |
| 4 | 0 | 1 | 200 | 0.70 s | 4,293 | **68** | 123, at 191 | no |
| 6 | 0 | 1 | 200 | 1.22 s | 2,923 | **125** | 60, at 185 | no |

`activity_partitions` read 1 on every queue of the control, so the dynamic config took effect.
No activity was retried.

### Start orders

The measured start orders, activity numbers in the order the worker started them.

Run 1, gap 20 ms, 4 partitions (largest overtaking 51):

```
0 2 4 1 7 8 5 9 10 14 12 13 17 3 22 27 20 11 19 36 24 38 37 25 28 41 6 39 34 42 50 43 40 53 15 55 46 51 58 56 45 48 54 57 60 61 62 16 59 18 63 65 67 66 21 68 23 80 69 26 92 95 72 70 73 29 30 31 79 32 100 106 110 81 113 71 115 33 75 35 44 96 47 49 78 128 130 132 52 82 84 133 88 89 135 90 136 97 98 139 99 91 102 93 94 64 107 149 108 101 74 103 104 112 154 76 77 83 159 114 165 117 116 120 166 167 124 126 85 119 169 174 127 142 121 144 146 86 87 105 109 122 111 175 150 153 155 123 157 125 163 134 140 168 143 170 180 191 171 193 147 118 151 131 129 152 161 177 183 184 137 162 189 197 179 141 199 145 173 182 186 187 138 148 156 192 195 158 198 160 164 172 176 178 181 185 188 190 194 196
```

Run 2, gap 0, 4 partitions (largest overtaking 155):

```
61 22 80 56 47 37 27 30 7 23 39 2 6 86 82 46 21 48 84 89 67 60 0 11 83 52 81 20 8 1 78 41 32 73 66 38 85 42 72 29 44 45 49 9 28 3 36 25 58 77 59 99 50 70 170 88 98 108 138 31 65 90 199 91 15 94 63 171 51 104 172 139 173 76 141 110 130 112 126 175 178 121 96 123 117 62 124 179 34 137 109 116 119 128 118 169 107 189 136 111 54 192 146 161 100 194 102 163 127 129 142 133 122 140 143 105 145 147 113 150 151 180 182 144 131 149 18 75 33 74 152 186 155 14 190 10 4 5 43 35 115 69 71 64 16 68 148 13 40 19 191 157 159 195 193 184 160 197 153 164 181 79 17 120 154 187 87 12 24 158 55 196 57 26 93 166 92 95 53 198 97 106 135 103 101 167 168 125 174 132 177 176 114 134 183 185 188 156 162 165
```

Control run 1, gap 20 ms, 1 partition (no overtaking), and runs 3 and 5 the same:

```
0 1 2 3 ... 197 198 199, every number in order
```

Control run 2, gap 0, 1 partition (largest overtaking 85):

```
1 70 57 69 33 92 59 28 81 3 68 88 9 52 2 62 87 36 54 48 55 30 91 63 90 4 10 98 94 61 47 53 82 12 27 38 25 72 45 78 93 86 76 29 19 51 64 0 32 5 67 46 13 8 80 74 60 89 84 77 56 85 37 79 14 75 21 24 44 73 95 83 17 15 22 43 42 35 65 16 6 41 40 18 66 71 49 58 20 23 39 7 31 34 26 50 11 99 97 96 141 129 154 142 130 143 199 132 144 155 131 156 146 145 133 157 158 134 136 135 159 160 137 138 161 147 150 139 162 148 163 149 140 188 181 164 182 151 184 189 190 185 186 191 183 165 194 187 193 192 167 195 168 166 198 170 197 171 196 169 100 101 102 153 172 104 103 174 173 177 178 152 175 105 179 180 176 107 123 116 106 125 124 115 117 118 108 119 111 109 112 120 121 110 122 113 126 114 127 128
```

The other runs' orders are in the PR that recorded this result.

## Findings

1. At the default of 4 partitions, order between workflow tasks is approximate: a task can be overtaken by up to a quarter of the 200 tasks scheduled after it (35 to 51), about one in ten pairs inverted (1,408 to 2,317 of 19,900).
   The four partitions each keep their own backlog and the worker's polls take from them in no fixed rotation.
2. At one partition, order between workflow tasks is exact: 0 inversions in 3 of 3 runs.
3. The sibling tasks of one workflow task reach the queue in no fixed order at either partition count (largest overtaking 68 to 157).
   In the one-partition orders the first hundred numbers start before the second hundred with few exceptions, which suggests the server hands the tasks of one workflow task to matching in groups of about 100 in parallel; this run does not test that reading.
   For placement this is order among the tasks of one job's layer, which no fairness rule of the epic depends on.
4. Nothing starved: 200 of 200 started in every one of the 12 runs, within 0.64 to 1.59 s of the worker starting, and no activity was retried.
   The eventual-run condition under a backlog whose wait passes the schedule-to-start ceiling stays #16's, as section 7 of the epic says.
5. The dev server of the `temporalio/temporal` pin runs the server's default of 4 partitions and takes `--dynamic-config-value` for the two partition keys, so #15 and #16 can reproduce either case at T1 with this code.

## Decision

The captain decided D14 as option (b): keep the server's default of 4 partitions and record approximate start order as the rule.

- Start order across jobs on a pool queue is approximate: a task scheduled apart is overtaken by up to 51 of 200, siblings scheduled together by 146 to 157, and no task starved.
- Correctness never depends on queue order: a task is ready only after every dependency is done (`settle` in `packages/workflow/src/dsy_workflow/ready.py`), so build before run holds under any order.
- A batch finishes at the same time under either order, but the finish order of single jobs is not predictable.
- The switch is one partition per pool queue, a Temporal dynamic config edit in #6's module, as the control run set `matching.numTaskqueueReadPartitions` and `matching.numTaskqueueWritePartitions` to 1.
  It gives first-in-first-out between tasks scheduled in different workflow tasks and still leaves the sibling tasks of one workflow task unordered.
- Move to it when something consumes each job's result as it finishes, or when several submitters share the pools and need a fair share per job.
  Then rerun the code above with the setting and record the throughput, which is unmeasured.
- Open for whoever makes the switch, and not verified here: whether the setting can be scoped to one queue, and how to lower the count safely on a queue with a backlog.
