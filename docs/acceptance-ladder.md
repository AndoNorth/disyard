# The test ladder

The levels of testing, the rules that keep each behaviour at one level, the recipe and budget of each level, and the order in which they gate each other.
The ladder is owned by epic #17.
The unit, integration and smoke levels are delivered.
Level 4, the end-to-end acceptance suite, is being delivered in pass 2, and this page states what it will hold so each piece is built against one description.

Which check to run for a change is in the [validation matrix](runbooks/validation-matrix.md).
The cluster rules are in the [operator runbook](runbooks/operator.md).

## The levels

| Level | What it proves | Owner | When it runs | Entry and budget | State |
| --- | --- | --- | --- | --- | --- |
| 1 Unit | One function or module in isolation, with no network and no containers | Each epic, for its own code | Every change, locally and in CI | `just test` | delivered |
| 2 Integration | One component against its real dependencies, without the whole cluster: Postgres, RabbitMQ, Temporal, the object store, the real compile toolchain | Each epic, for its own component | Every change in CI, and locally for one domain | `just itest [domain]` | delivered |
| 3 Smoke | After a deploy every layer answers and one level 0 job produces the known answer | Epic #17 owns the suite, and the cluster epic owns `just cluster-verify` as its infrastructure half | After every `just up` and deploy, and first in every higher level | `just smoke`, 300 s budget | delivered |
| 4 End to end | The platform works on public surfaces: a level 1 job with several result blocks, the native aggregate, observability, and one canonical case of each negative and control behaviour | Epic #17, pass 2 | Nightly or locally, and before the proof of concept is called done | `just e2e`, about 10 minutes, a proposal until a first measured run | harness delivered, scenarios not, so `just e2e` fails with `FAIL scenarios none collected` |
| 5 Failure and recovery | The guarantees hold under injected failures | Epics #15 and #18, with the [failure drills](runbooks/drills/README.md) for the observable half | On demand | The drills and #15's scripts | drills delivered |
| 6 Load | Behaviour at saturation: caps hold, no job is lost or duplicated | #14 provides the tool and #16 owns the runs | Before the soak, on a host sized for it | `just load-test`, hours | not delivered, exits 3 |
| 7 Performance | Numbers: throughput, latency, compile cost | #16 | On demand | `just characterize`, hours | not delivered, exits 3 |
| 8 Soak | Durability and correctness over days | #16 | A weekend run | Days | not delivered |

Each budget is enforced by a timer in the recipe, so a slow level fails loudly and is not skipped.
A time here is a budget or a proposal, and a measured one lives in the [validation matrix](runbooks/validation-matrix.md).

## The rules

1. A behaviour is proven at the lowest level that can show it, by the epic that owns the code.
   Cache semantics are proven at integration level, and the e2e asserts one canonical hit.
2. A failure that only a higher level catches is a gap in a lower level.
   The fix adds a lower level test, and the e2e does not grow variants.
3. Smoke is a gate and not a behaviour test.
   It stays fast and green whenever the platform is healthy, and it uses a level 0 workload so a failure points at the platform and not at a workload.
   Pass 2 does not add rows to it.
4. Level 0 workloads are the shared fixtures of the integration, smoke and e2e levels, because their outputs are known values.
5. A level only runs when the level below it is green.
   `just e2e` runs the smoke gate first under the lock it already holds, and a characterization baseline is only accepted from a run that started on a green smoke.
6. One canonical case per negative behaviour at level 4, and each case names the lower level test that proves its variants.
   Where none exists, a lower level test is added and the e2e does not absorb the variants.
7. Assertions use public surfaces where possible, and every direct read of an internal store is listed in [Direct reads](#direct-reads).
8. Stored hashes are compared with manifests, and parsed result values with values the host recomputes from the contracts and the kernel.
   Builds are not byte reproducible and XML bytes are not a contract, so neither is compared against a fixed value.
9. Names are read from the contracts (`naming()`, `contracts/artifact/layout.yaml`, `contracts/events`) and not written as literals, so a rename edits no test logic.
10. Waiting is polling with a timeout and never a fixed sleep, because the observability backends lag.
11. Counters are asserted as a change from a quiet baseline.
    The cluster lock is what makes the baseline quiet, because a job counter has no tenant label.
12. Every run uses unique ids, a batch key of the form `e2e-<12 hex>`, under a tenant of its own, so runs cannot affect each other.
    The tenancy profile of preset `ci` has the tenant `e2e` for the acceptance run, next to `acme`.

## Recipes, gates and the lock

| Recipe | Does | Needs | Exit codes |
| --- | --- | --- | --- |
| `just test` | Unit tests of the whole tree, or `scripts/test.sh <path>` for one package | the flake | 0 or the failing test run's code |
| `just itest [domain]` | Integration tests against containers started by `dsy_itest` | Docker | as above, 4 refused: the T1 lock wait ran out |
| `just ci` | Everything CI's `check` job runs | Docker | as above, 4 refused: the T1 lock wait ran out |
| `just smoke` | The smoke gate, see [the rows](runbooks/validation-matrix.md#the-smoke-rows) | a running `ci` cluster | 0 ok, 1 a row failed or the budget ran out, 2 usage, 4 refused |
| `just e2e` | Level 4: the smoke gate first, then the scenarios, in a 600 s budget | a running `ci` cluster with the observability stack on | 0 ok, 1 a scenario or smoke failed or the budget ran out, 2 usage, 4 refused: no cluster, the stack off or the lock not taken |

Exit 3 is what a stub returns on purpose, so a script never mistakes an undelivered level for success.

Every cluster recipe takes `/tmp/dsy-cluster.lock` and waits up to 900 s for it, unless `DSY_CLUSTER_LOCK_HELD` says the caller holds it.
A wait that runs out is a refusal (exit 4) and not a failure, so a queued gate is not mistaken for a failed one.
`just e2e` takes the lock once, exports `DSY_CLUSTER_LOCK_HELD`, and run smoke as its first step under that hold.
A run of about 10 minutes is under the 900 s wait of a queued lane, and a slow run can exceed it, which that lane sees as a refusal.

The integration tests of `just itest` and `just ci` queue the same way for the two T1 lock files of `dsy_itest` and wait up to 1800 s.
A wait that runs out is also a refusal (exit 4, a message beginning `refused:`), see [T1 lock](dev-setup.md#t1-lock).

The gate order is: `just ci` on the change, a cluster from `just up` and the module deploy, `just cluster-verify`, `just smoke`, then `just e2e`.
Load, performance and soak need a green smoke on the same deploy.

### The flake loop

The proof of concept is called done after ten consecutive e2e passes on one deploy.
The loop holds the cluster lock once for all ten passes and runs only in the exit-test window, announced to the other lanes, because releasing the lock between passes would lose "consecutive on one deploy".
It prints one summary line per pass and a pass count, and stops at the first failure.

### Where it runs

The e2e needs the observability stack for its query step, so its home is preset `ci` with `DSY_OBS=on`, which needs a host `MemTotal` of 15,488 MiB (`obs_memory_mib` in `deploy/presets/ci.yaml`).
Nothing else may use memory heavily during a run, and a `just itest` beside the cluster risks an out-of-memory kill of a node.
Changing the preset or the stack value costs a `just nuke`, a `just up` and a module deploy, so a session does not alternate configurations.

No CI job runs the e2e.
The stack is never deployed on a hosted runner, and `ci` alone is 12,032 MiB on a runner of about 15,983 MiB.
It runs nightly or locally, and the decision is taken again from the measured local run.

## Scenarios of level 4

The first live run of each scenario replaces its estimate, and the budget is then set from measurement, as the smoke budget was.
A scenario that changes shared state restores it in a `finally`, and runs last, so a failed run leaves the cluster as `just smoke` expects.

| Id | Scenario | One canonical assertion | Lower level proof of the variants |
| --- | --- | --- | --- |
| H1 | The acceptance job: two chains of compile then simulate, and one native aggregate that waits for both, over level 1 workloads with distinct seeds | Status runs accepted, admitted, running, succeeded. A and B are two distinct bundles and both cache misses. Each wasm sits under its own sha256 and equals the hash in its `build-info.json`. Parsed blocks equal the blocks the host recomputes for that seed. The `summary.json` equals the summary the host computes from both seeds | the workflow and aggregate suites of `just itest` |
| H2 | Query: status and Temporal history after H1, with the stack on | The completed counter rose by one from a quiet baseline. Every lifecycle event in the history is a valid CloudEvent matching its `dataschema`, and a result ready event exists per simulate and for the aggregate. One trace holds spans from the CLI, dispatch, admission and the workflow start. Logs for the job id exist from each component | the catalogue contract test and the [live check](runbooks/drills/live-check.md) |
| N6 | Compile cache hit: H1's bundle again as a new job with another seed | No compile attempt, the simulate uses the same wasm hash, and the blocks are those of the new seed | the cache tests of the compile and workflow packages |
| N1 | Failing compile as chain B, and a variant that allows partial results | Chain B stops without a retry, chain A finishes, partial results are kept, aggregate does not run, and the job ends `failed` with a typed reason. The variant runs aggregate on A and ends `partial` | the workflow failure tests |
| N2 | A simulate that writes a result block outside the schema, through the `bad_block` fault mode | The aggregate fails with `aggregate_parse_failed`, which is not retried and names its input, and the job ends `failed` with a typed reason | `packages/aggregate/tests/test_hardening.py` |
| N3 | The same tenant and submission key twice | One job row, one workflow and one `duplicate` answer | the dispatch API suite |
| N4 | A malformed intake message, and an event with an unsupported major version | Both land in the dead-letter queue with a reason code and the original message, and another job is unaffected. The queue is purged at the end, inside the delay of the dead-letter alert | `dsy_broker.suite deadletter` |
| C1 | Pause and resume of a running paced job | Status `pausing` then `paused`, the slot is released, and the resumed job finishes. Finishing from the checkpoint waits for the checkpoint epic | the dispatch driver's `pause-resume` scenario |
| N5 | Cancel before admission, with admission held at zero replicas | A job cancelled while `accepted` never starts, so no workflow exists. Runs last | the dispatch driver's `cancel-before-start` scenario |

One order of a run is: smoke gate, H1, H2, N6, N1, N2, N3, N4, C1, N5.

Not in the e2e:

- Two compile pods on different nodes with overlapping attempts.
  Preset `ci` has one compile node and one compile pod, so it is checked later in a local fixture or a load test.
- A pause after a checkpoint, and a resume with a patched bundle, until the checkpoint epic delivers it.
- Injected infrastructure faults, which stay with the failure drills.

## Direct reads

The e2e reads these through a surface other than the dispatch API and the observability read APIs.
Anything not in the table is read through the API or the stack.
Every row is read-only.
The smoke gate reads none of them, and adds no row to this table.

| Read | Through | Why it is not a public surface |
| --- | --- | --- |
| The job row, the events of a job (`events_for_job`) and the compile cache entry row that names a wasm and its build-info | a named read-only Postgres client of `dsy_acceptance`, with a read-only login | The accepted row existing before the publish is confirmed, and one job row for a repeated key, are not visible through the API |
| Workflow history and describe, search by workflow id | the Temporal client | One workflow per job, and the lifecycle events in the history |
| The wasm objects and their build-info | the object store, with the `workflow` role as an extra read-only identity (get and list on sources, components and results) | The read-only `export` identity has the results bucket only |
| Stored results, `results.xml` and `summary.json`, by reference | the object store, with the `export` identity | Results are read by the references the API returns |
| The dead-letter queue, its letters with their reason codes and bodies, and the purge of N4's own letters | the broker client of `dsy_acceptance` and the dead-letter tool, with the `dispatch` broker user publishing the two poison events to the intake queue | A rejected intake message is visible only in the queue, and a malformed event cannot be submitted through the API |
| Pods of the pool and process lists | `kubectl exec` | The aggregate runs as a native task with no `wasmtime` process, which no API shows |
| Admission replicas | `kubectl scale`, a write | Held at zero by N5 so a job stays `accepted`, and restored in a `finally` |

N5 and N4 are the two scenarios that write shared state, which is why they run last and restore it.

## Not covered

- Load, performance and soak, see levels 6 to 8.
- A UI for results.
- Infrastructure faults: killing a pool, filling the store.
  The [failure drills](runbooks/drills/README.md) hold them.
