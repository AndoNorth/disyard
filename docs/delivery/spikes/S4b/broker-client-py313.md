# Spike: the S4 checks on Python 3.13, and what S4 never ran (S4b)

Run on 2026-11-16.
Owner: the [broker epic](https://github.com/AndoNorth/wasm-dag-forge/issues/7), lane S, filed as [spike issue 400](https://github.com/AndoNorth/wasm-dag-forge/issues/400).
Spec: [S4b in docs/delivery/epics/10-broker.md](../../epics/10-broker.md#s4b-the-s4-checks-on-python-313-and-what-s4-never-ran), which gates 0.2 and M.1.
Method: [docs/delivery/method.md](../../method.md).
Throwaway code: [code/](code/), run as `nix develop -c docs/delivery/spikes/S4b/code/run.sh [out-dir] [--only-checks|--only-measure]`.
Every number below is a `figure <name> <value>` line of that run.

## Result

**Pass.**
Checks 1 to 5 of S4 pass with 41 of 41 assertions in three consecutive runs on Python 3.13.15, on proton 0.40.0 and also on proton 0.41.1.
Proton 0.40.0 builds from its sdist under `uv` in the dev shell and in a build stage on the pinned `python` image.

One condition is attached to the dev shell half of the pass line: the dev shell has no C compiler of its own (`mkShellNoCC` in `flake.nix`), and the build used the host's `/usr/bin/cc`.
With every non-Nix directory removed from `PATH` the build fails with `No such file or directory: 'gcc'`, and with `gcc` 15.3.0 of the pinned nixpkgs added it builds and imports.
So 0.2 adds `gcc` to the dev shell, as the shared-file rules of section 3 foresee, and no header or library is needed beyond it.

| Default | What this run says | Change |
| --- | --- | --- |
| D14 limit 256 MiB, watermark 154 MiB | the broker's own memory reaches 160.5 MiB while it recovers its full queues after a restart, and raises the memory alarm | **changes**: limit and request 320 MiB, watermark 192 MiB, raised before M.1 |
| D6 delayed retry | 4.4.0 offers it on quorum queues and the counts stay as S4 measured | **changes**: `wft-intake` gains the delayed retry, type `failed` recommended |
| D15 series | ready and unacked are in `queue_coarse_metrics`, the consumer count is not | **changes**: the consumer count is family `queue_consumer_count` |
| D10 client | a refused permission ends the whole session, and credit sent before the attach is answered ends it too | **adds** two rules for C.1 |
| Q1 (a) premise | a held last delivery lost to a dropped connection, a client close or a restart dead-letters as `delivery_limit` | confirms |
| C.2 expected count | a held delivery lost the same ways comes back with its count raised by one | sets it to 1 from 0 |
| Q4 (a) `release()` | goes back to the head with its count unchanged | confirms |
| D17 browse | `released` counts nothing and keeps the order when the pass settles in delivery order | confirms, with that condition |
| D5 `wft-dlq` | `delivery-limit` -1 turns the limit off | confirms, value `-1` |
| D19, D12 | a definitions directory of a topology file and a users file with salted hashes imports, applies the policies and creates no `guest` | confirms |
| Q2, D22 | proton 0.41.1 passes the same checks | confirms (a), (b) stays available |

## Question

Does the S4 wrapper design hold on Python 3.13 with proton 0.40.0 built from its sdist, against `rabbitmq` 4.4.0 pinned by digest, and what do the settings of D5 to D19 that S4 never ran do?

## Environment

| Item | Value |
| --- | --- |
| Host | WSL2, 20,002 MiB `MemTotal` (`host.mem_total_mib`), 8 CPUs (`host.cpus`), Docker 29.1.3 (`host.docker`) |
| Other work on the host | 1 other container (`host.other_containers`), not touched, 16,218 MiB `MemAvailable` before the run (`host.mem_available_mib.before`) |
| Locks | one `wft-t1` lock file through `wft_itest.Session(containers=1)` for each broker, one broker at a time, no cluster, ports on slot 4 (`WFT_SLOT` from the pool slot of the worktree, because a sibling worker held slot 0's ports) |
| Broker | `rabbitmq:4.4.0@sha256:1166559c9e77b0208b9d96bfaf265ddbdd491ce3cebb8cf990aabe9274f16d2c` (`image.rabbitmq`), the digest 0.2 pins, a fresh container for each run and each measurement, capped at 1 GiB except for check 10 |
| Broker configuration | the settings of D14 (`vm_memory_high_watermark.absolute`, `disk_free_limit.absolute` 64 MB, `raft.wal_max_size_bytes` 32 MB, `+S 1:1 +sbwt none +sbwtdcpu none +sbwtdio none`), `enabled_plugins` of `[rabbitmq_prometheus]`, and a definitions directory imported at boot (check 8) |
| Topology | `wft.intake` direct, `wft.dlx` fanout, quorum queues `intake.acme.standard`, `intake.acme.low`, `intake.beta.standard`, `intake.beta.low` and `intake.dlq` with initial group size 1, policies `wft-intake` and `wft-dlq` of D5, and for the full-queue case a queue `intake.acme.tiny` under a policy `wft-tiny` of higher priority with `max-length` 3 |
| Users | `dispatch`, `admission` and `operator` with the permissions of D12 and fresh passwords per broker; each check publishes as `dispatch`, consumes intake as `admission` and reads and replays the dead-letter queue as `operator` |
| Python | 3.13.15 of the dev shell, a scratch venv per proton version created with `uv` 0.11.21 |
| Client | `python-qpid-proton` 0.40.0 and 0.41.1, each built from its sdist with `--no-binary`, with `cffi` 2.1.1; neither build links OpenSSL or Cyrus SASL, and SASL PLAIN works without them |
| Build stage | `python:3.13.15-slim-trixie@sha256:7c61056e…` of `images.python`, see [code/Dockerfile](code/Dockerfile) |
| Message | the golden `job.accepted` 1.0 event of `contracts/events/vectors/golden/`, 803 bytes, as one `data` section with `content_type` `application/cloudevents+json; charset=utf-8`, durable, no application-properties; the poison `not-json.json` for an application dead letter |

## Method

- [code/amqp.py](code/amqp.py) rebuilds the S4 wrapper from `broker-client.md` in the shape of D10: one proton `Container` thread per connection, calls injected into it, receivers at credit 0 with only `flow(n)` (`pull`) and `drain(n)`, `ack`, `retry`, `dead_letter(reason, detail)`, `release()` and the browse settle `released`, no reconnect.
- [code/broker.py](code/broker.py) renders the configuration and the definitions directory and starts one broker container under a T1 lock.
- [code/s4b.py](code/s4b.py) runs the five checks with 41 assertions, and the measurements.
- [code/run.sh](code/run.sh) builds each proton version under `uv` and in the build stage, runs the checks three times per version against a fresh broker each time, and the measurements once on 0.40.0.
- Queue counts are read with `rabbitmqctl list_queues` and polled, as S4 finding 8 asks.
- The run was recorded twice: run 1 with every check and measurement, and run 2 on the committed code after formatting, with the checks of both versions and check 10 at 256 MiB; both gave 41 of 41 in all twelve runs, and the tables below take run 1 except where they say otherwise.
- Byte identity compares the raw encoded properties and data sections, split from the received transfer by their descriptors, and checks that no application-properties section appears.

## Measured numbers

### Builds (pass line)

| Figure | 0.40.0 | 0.41.1 | Names |
| --- | --- | --- | --- |
| Under `uv` in the dev shell | ok in 12.6 s | ok in 11.8 s | `proton<v>.devshell.build`, `.build_s` |
| Compiler the dev shell offers | none of its own, `/usr/bin/cc` of the host | the same | `devshell.cc`, `devshell.cc_from_nix` |
| With non-Nix `PATH` entries removed | fails, `No such file or directory: 'gcc'` | not run | a trial command, not a figure |
| With `gcc` of the pinned nixpkgs added | ok, imports `(0, 40, 0)` | not run | a trial command, not a figure |
| Build stage on `images.python` | ok, 21.1 s for the wheel step cold, then a cache hit in the recorded run | ok in 24.5 s | `proton<v>.image.build`, `.build_s` |
| Import in the runtime stage, which has no compiler | `0.40.0/py3.13.15` | `0.41.1/py3.13.15` | `proton<v>.image.import` |
| Build stage without `gcc libc6-dev` | fails, `No such file or directory: 'gcc'` | not run | a trial command, not a figure |

The build stage installs `gcc` and `libc6-dev` with `apt-get` and builds the wheel with `pip wheel --no-binary`; the runtime stage installs only the wheel, and the runtime image is 134 MB.

### Checks 1 to 5 (pass line)

| Check | Result, every run of both versions | Names |
| --- | --- | --- |
| 1 Publish outcome observed | routable `accepted`, unroutable key `released`, a queue of three takes 4 `accepted` then 4 `rejected`, as S4 saw; a missing queue is a refused attach `amqp:not-found` that leaves the connection usable | `.check1.*` |
| 2 One message per credit | credit 1 gives 1 with the broker at ready 2 and unacked 1, credit 2 gives 2, nothing extra after 2 s, a drain on an empty queue returns nothing in 4 to 39 ms | `.check2.drain_empty_s` |
| 3 Retry counts | counts 0, 1, 2, 3 over four retries, and 0, 0, 0, 0 over four `release()` | `.check3.retry_counts`, `.release_counts` |
| 4 Dead-letter on the sixth failed delivery | counts 0 to 5, no seventh delivery, `x-last-death-reason` `delivery_limit` with no application reason; an ack of the sixth leaves the dead-letter queue empty; `dead_letter` puts `x-opt-wft-reason` and `x-opt-wft-detail` on the dead letter in the same settle, with broker reason `rejected` | `.check4.*` |
| 5 Byte-identical after dead-letter and replay | 803 bytes, properties and data sections identical as sent, at intake, in the dead-letter queue and after replay; the replay carries only `x-exchange` and `x-routing-key`; the negative control, a resend of the whole dead letter, carries the nine stale `x-*death*` and `x-opt-wft-*` annotations, and a changed byte fails the body check | `.check5.*` |

| Run | 0.40.0 | 0.41.1 |
| --- | --- | --- |
| 1 | 41/41 | 41/41 |
| 2 | 41/41 | 41/41 |
| 3 | 41/41 | 41/41 |
| Verdict | pass | pass |

### 6 A delivery held across a dropped connection or a restart

One message is pulled and held unsettled, then the connection is lost in one of three ways, and a new `admission` connection reads where the message went.
"Held at count 5" first retries the message five times, so the held delivery is the last one.

| Cause | Held at count 0 | Held at count 5 | Error on the old connection |
| --- | --- | --- | --- |
| broker closes the connection (`rabbitmqctl close_connection`) | back on its queue at count 1 | dead-lettered, `x-last-death-reason` `delivery_limit` | `BrokerUnavailable`, condition `amqp:internal-error` |
| client closes its connection without settling | back on its queue at count 1 | dead-lettered, `delivery_limit` | none, the client closed it |
| broker restart (`docker restart`) | back on its queue at count 1 | dead-lettered, `delivery_limit` | `BrokerUnavailable`, condition `amqp:internal-error` |

A lost held delivery counts as a failed delivery on 4.4.0, so the premise of Q1 holds: the broker's own `delivery_limit` death occurs exactly when a last delivery is lost, which is the `delivery_limit` row of the reason table.
C.2's `held_delivery_across_restart` and `held_delivery_across_connection_drop` expect the count to rise by one.

### 7 Delayed retry

RabbitMQ 4.4.0 takes three policy keys on quorum queues, `delayed-retry-type` (`all`, `failed`, `returned` or `disabled`), `delayed-retry-min` and `delayed-retry-max` in milliseconds, all found in `rabbit_quorum_queue` and applied by `rabbitmqctl set_policy`.
The runs set `wft-intake` with the keys of D5 plus a minimum of 1,000 and a maximum of 30,000, then retried one message six times and released another once.

| Type | Counts over six retries | Seconds before each redelivery | Dead letter | Seconds before a released message returns | Its count |
| --- | --- | --- | --- | --- | --- |
| `all` | 0, 1, 2, 3, 4, 5 | 1.0, 2.0, 3.0, 4.0, 5.0 | yes, `delivery_limit` | 1.0 | 0 |
| `failed` | 0, 1, 2, 3, 4, 5 | 1.0, 2.0, 3.0, 4.0, 5.0 | yes, `delivery_limit` | 0.0 | 0 |
| `returned` | 0, 1, 2, 3, 4, 5 | 0.0 each | yes, `delivery_limit` | 1.0 | 0 |
| `disabled` | 0, 1, 2, 3, 4, 5 | 0.0 each | yes, `delivery_limit` | 0.0 | 0 |

The delay is the minimum times the delivery count, up to the maximum, so the five counted retries of one message span 15 s before it dead-letters, where without the delay they took milliseconds.
`failed` delays only a counted retry (`modified` with `delivery-failed=true`), which is the case D6 names, and leaves `release()` immediate; `all` also delays a release by a second.
A message under a delay is not available, so a `drain` does not wait for it, and a dead letter by `at-least-once` arrives asynchronously, so a test of either polls.

### 8 Definitions import

| Item | Value | Names |
| --- | --- | --- |
| Import | `definitions.local.path` set to a directory holding `topology.json` and `users.json`, both imported at boot | `m8.*` |
| Users | exactly `admission`, `dispatch`, `operator`, with `rabbit_password_hashing_sha256` hashes of a 4-byte salt | `m8.users` |
| `guest` | not created, a login is refused with `amqp:unauthorized-access` | `m8.guest_login` |
| Permissions | as D12, configure `^$` for all three | `m8.permissions.*` |
| Policies | `wft-intake` and `wft-dlq` with every key of D5 on every intake queue and on `intake.dlq`, read back from the broker | `m8.policy.*`, `m8.queue.*` |
| Exchanges, bindings | `wft.intake` direct, `wft.dlx` fanout, both durable, 6 bindings | `m8.exchange.*`, `m8.bindings` |
| Refusals | `dispatch` attaching a receiver, `admission` publishing, `operator` publishing to `wft.dlx`: each ends the session with `amqp:unauthorized-access` | `m8.refused.*` |

### 9 The dead-letter policy and a browse

| Figure | Value | Names |
| --- | --- | --- |
| Effective `wft-dlq` | `delivery-limit` -1 | `m9.dlq_effective` |
| 30 counted retries across five dead letters, six each | all five stay, counts rise 1 to 6, nothing dropped | `m9.dlq_retries_survived`, `m9.dlq_retry_counts` |
| 25 browse passes, each pulling the five and settling `released` in delivery order | the same order every pass, header count and `x-delivery-count` stay 1, `x-acquired-count` rises 1 to 24 | `m9.browse_*` |
| A pass that settles the head last | the next pass sees `12340` instead of `01234` | `m9.order_after_out_of_order_release` |

### 10 Memory at the D14 limit with full queues

Each of four intake queues holds 1,000 messages and `intake.dlq` 1,000 dead-lettered ones (5,000 publishes, all `accepted`), then the broker restarts and recovers them.
The working set is `memory.current` less `inactive_file` of the container sampled every 50 ms; the broker's own figure is `rabbitmq_process_resident_memory_bytes` of `/metrics`, the one the watermark compares (calculation strategy `rss`), sampled every 0.5 s.

| Figure | 256 MiB, watermark 154 MiB | 320 MiB, watermark 192 MiB | Names |
| --- | --- | --- | --- |
| Fill: peak working set | 129.3 MiB | 128.9 MiB | `m10.<limit>.fill.peak_working_set_mib` |
| Fill: peak broker memory | 147.9 MiB | 147.6 MiB | `.fill.peak_broker_rss_mib` |
| Restart with full queues: peak working set | 215.0 MiB | 216.1 MiB | `.restart.peak_working_set_mib` |
| Restart: peak broker memory | **160.5 MiB** | 160.2 MiB | `.restart.peak_broker_rss_mib` |
| Memory alarm raised | **yes, during the restart**, cleared by the end | never | `.<phase>.watermark_alarm_values` |
| Ready after restart | 5.4 s | 6.4 s | `.restart_ready_s` |
| `memory.peak`, page cache included | 222.8 MiB | 225.7 MiB | `.memory_peak_mib` |
| OOM kills | 0 | 0 | `.oom_kill` |
| Broker memory at rest after recovery | 153.3 MB, `reserved_unallocated` 59 percent of it | 154.7 MB, `reserved_unallocated` 59 percent of it | `.status.total_memory_used`, `.memory_breakdown_top` |

The 256 MiB column is the confirming run on the committed code, with the watermark at 154 MiB as D14 states.
The first recorded run set it at 153 MiB by rounding down and gave the same picture: fill 149.3 MiB, restart 160.7 MiB with the alarm raised, working set 212.8 MiB, no OOM kill.
Neither limit came near an OOM kill: the risk at 256 MiB is the alarm, which blocks every publisher, not the container cap.

### 11 Series of the detailed endpoint

| Family | Series | Labels |
| --- | --- | --- |
| `queue_coarse_metrics` | `rabbitmq_detailed_queue_messages_ready`, `rabbitmq_detailed_queue_messages_unacked`, `rabbitmq_detailed_queue_messages`, `rabbitmq_detailed_queue_process_reductions_total` | `vhost`, `queue` |
| `queue_consumer_count` | `rabbitmq_detailed_queue_consumers` | `vhost`, `queue` |
| both | `rabbitmq_detailed_queue_info` (`queue_type`, `membership`), `rabbitmq_build_info`, `rabbitmq_identity_info` | |

With one message ready and one held by a consumer on `intake.acme.standard`, the endpoint read ready 1, unacked 1, messages 2 and consumers 1, eight seconds after the pull.

### 13 Where `release()` puts a message

| Case | Order of the next pull | Counts |
| --- | --- | --- |
| A, B, C queued, A pulled and released | A, B, C | 0, 0, 0 |
| A, B, C queued, A pulled and retried | A, B, C | 1, 0, 0 |
| A, B, C, D queued, A and B pulled, B released then A | B, A, C, D | |

A message settled back with `modified`, counted or not, returns ahead of every message never delivered, in the order the messages were settled back, and `release()` leaves its count unchanged.

## Findings

1. **The broker needs 320 MiB, not 256.**
   At 256 MiB the fill alone took the broker to 147.9 MiB of its 154 MiB watermark, and recovering the full queues after a restart took it to 160.5 MiB and raised the memory alarm, which blocks every publisher until the alarm clears.
   At 320 MiB with the watermark at 0.6 of it, 192 MiB, the same restart peaks at 160.2 MiB and no alarm is raised, with the working set at 216.1 MiB of 320.
   Most of the broker's figure is allocator memory held after use (`reserved_unallocated` 59 percent at rest), so it does not fall back after a peak.
   By D14's rule the limit and the request move up one 64 MiB step to 320 MiB with `vm_memory_high_watermark.absolute` 192 MiB, which changes the RabbitMQ reserve row of `scripts/cluster/budget.sh` from 256 to 320 MiB and takes 64 MiB of the 566 MiB that section 8 counts free on the `ci` infra node.
   Section 8 asks for this to be raised before M.1 starts.
2. **The dev shell needs `gcc`.**
   The dev shell is `mkShellNoCC`, so the build relied on the host compiler, and a host without one fails.
   0.2 adds `pkgs.gcc` of the pinned nixpkgs, which builds proton 0.40.0 with no other package.
   An image needs `gcc` and `libc6-dev` from `apt-get` in its build stage only.
3. **The delayed retry exists, and `failed` is the type that matches D6.**
   It spaces counted retries 1, 2, 3, 4 and 5 s apart with the minimum at 1 s and leaves the counts as S4 measured, so D6's condition holds and `wft-intake` gains `delayed-retry-type` `failed`, `delayed-retry-min` 1000 and `delayed-retry-max` 30000.
   The body's type `all` also delays a `release()` by a second, which Q4 does not need, and either type changes nothing a consumer parses.
   With it, six transient failures need 15 s to dead-letter a healthy job instead of milliseconds, which is the risk D6 records.
4. **Two client rules for C.1.**
   A refused permission is answered by ending the whole session with `amqp:unauthorized-access`, not by detaching the one link, so every link of that session fails with it.
   Credit sent on a receiver before the broker answers its attach can land on a handle that a refused attach has already detached, and the broker then ends the session with `amqp:session:unattached-handle`; the wrapper waits for the attach answer before it grants credit, and a missing queue then raises `TopologyError` with the connection still usable.
   proton's `MessagingHandler` closes the whole connection on any link error and adds a flow controller, so the wrapper is a plain `Handler`.
5. **A lost held delivery counts.**
   A delivery held when its connection drops, its client closes or the broker restarts returns with its count raised by one, and on the last delivery it dead-letters as `delivery_limit`, which confirms Q1's premise and sets the counts that C.2 asserts.
   The old connection raises `BrokerUnavailable` with `amqp:internal-error` when the broker closes it or restarts.
6. **The consumer count is a family of its own.**
   `queue_coarse_metrics` carries ready and unacked but not the consumer count, which is `rabbitmq_detailed_queue_consumers` of family `queue_consumer_count`, so the suite's `check` guard and `WftIntakeQueueStalled` scrape both families.
7. **A browse keeps the order only when it settles in delivery order.**
   A message settled back, `released` or `modified`, goes ahead of the messages never delivered in the order it came back, so a pass that releases its holds in the order it got them leaves the queue as it was, and `released` changes neither the header count nor `x-delivery-count`.
   `release()` in admission therefore puts a message back at the head, which Q4 (a) assumes.
8. **What S4 found still holds.**
   The full queue takes one message over `max-length` before it rejects, the replay carries only `x-exchange` and `x-routing-key`, and a resend of the whole dead letter carries its stale death annotations.

## Caveats and what was not measured

- No check of the S4b block needs the cluster, so none was skipped for it; the broker in a pod with its PersistentVolumeClaim and a pod deletion are M.1's and the exit test's.
- Check 10 runs in Docker with a cgroup v2 memory limit, as S11 did, and not under the kubelet; the restart is `docker restart` of the same container, which keeps its data volume as a pod restart keeps its PersistentVolumeClaim.
- One host and one measurement run; the checks ran three times per version.
- The proton 0.40.0 build stage was a cache hit in the recorded run after the cold build of the trial (21.1 s for the wheel step), and 0.41.1 was cold.
- The dev shell without the host compiler and with the pinned `gcc` was run for 0.40.0 only.
- Not measured: TLS, more than one node, concurrent consumers on one queue, publishes from several threads on one connection (C.1's `publish_from_threads`), the consumer timeout of the broker, and a `release()` loop under the `all` type over many outages.
- Two worktrees that both leave `WFT_SLOT` unset share the T1 ports of slot 0, so two T1 sessions on one host can collide on a port even when each holds its own lock file; this run used slot 4, and the collision is outside this spike.
