# dsy-acceptance

Acceptance of a deployed cluster from the host, imported as `dsy_acceptance`.
It holds the smoke gate and the small clients it drives the platform through, and the e2e suite is written over the same clients.
It builds on `dsy_clusterkit` for every cluster call: forwards, kubectl, readiness, polling and credentials.

It is test tooling, so no service depends on it and no image installs it.
The dev group of the workspace installs it.

## Dependencies

`pyproject.toml` declares every dependency the clients and the gate import: `psycopg[binary]`, `temporalio`, `dsy-broker`, `dsy-clusterkit`, `dsy-eventstore`, `dsy-jobstate`, `dsy-kernel`, `dsy-keys`, `dsy-obs`, `dsy-storecheck`, `dsy-temporal`, `dsy-workflow` and `dsy-workloads`.
`tests/test_wheel.py` builds the wheel, installs it with only those dependencies into a clean virtual environment and imports every module of the package, so a module that imports an undeclared dependency fails there.

## State

`dsy_acceptance.report` is delivered: the row line, the count line and the exit codes of the smoke recipe contract.

- `ok(name, detail)`, `fail(name, detail)` and `skip(name, reason)` return a `Row`, whose `line` is `ok|FAIL|skip <name> <detail>`, the format of `cluster-verify`.
- `count_line(preset, rows, elapsed_s)` is `smoke <preset>: <n> ok, <n> failed, <n> skipped, <s> s`, and every row counts in exactly one bucket.
- `exit_code(rows, passed_through=None)` is 0 when every row is ok and 1 otherwise, or `cluster-verify`'s usage exit 2 or refusal exit 4 when one of those ended the run.

The clients and the gate are delivered, each under `src/dsy_acceptance/`:

- `clients.api`: `request`, `stream`, `template`, `batch`, `batch_key`, and `Api(dispatch, control, tenant)` with `upload_source`, `submit_one`, `status` and `cancel_batch`.
- `clients.store`: `fetch(uri, identity, endpoint) -> bytes`, signed as the store role the caller names.
- `clients.temporal`: `connect(address) -> Client`, and `describe`, `history` and `executions` of a job's workflow by workflow id.
- `clients.postgres`: `connect(address)` a read-only session, `job(conn, job_id)` and `events(conn, job_id)`.
- `clients.observability`: the `dsy_obs.query` reads `prom`, `targets`, `loki`, `trace` and `profiled`.
- `clients.pod`: `exec_in(context, namespace, pod, *argv)` and `process_names(context, namespace, pod)`.
- `clients.broker`: `dead_letters(address) -> int`.
- `smoke`: `python -m dsy_acceptance.smoke`, the ordered driver of the gate run by `scripts/smoke.sh`, with `smoke.telemetry` for the observability row and `smoke.results` for the push of the verdicts to the smoke dashboard.

`dsy_acceptance.fixtures` holds the inputs of the e2e suite, with no cluster needed:

- `bundles.variant(name)` and `bundles.missing_entry()`: the workload library bundle of a variant, and a bundle whose entry module `MISSING_ENTRY` has no `Run` class, which the compile task refuses as `compile_failed`.
- `jobs.two_chains`, `jobs.failing_second_chain` and `jobs.two_seed_summary`: two compile then simulate chains under one `digest_list` aggregate, the same with chain `b` failing its compile, and the summary a host expects over two seeds.

`dsy_acceptance.e2e` is the harness and the scenario logic of the e2e suite of the [test ladder](../../docs/acceptance-ladder.md), with one module for each of H1 and N6, H2, N1 to N5 and C1, whose checks run against fakes in `tests/` and against the cluster in `tests/e2e/`:

- `e2e`: `TENANT`, `ORDER` (the run order of the scenario ids), `batch_key()` (`e2e-<12 hex>`) and `wait(check, timeout_s, what)`, which polls until a truthy value and never sleeps a fixed time.
- `e2e.baseline.Baseline`: a counter read before a scenario and the change after it, asserted from a quiet baseline.
- `e2e.summary.Summary`: one `ok|FAIL|skip <scenario> <detail>` line per scenario and a count line, in the smoke row format.
- `tests/e2e/conftest.py`: the `@pytest.mark.scenario("<id>")` marker, the run order, and the fixtures `client`, `batches` (a fresh batch key per call, cancelled when the test fails), `prometheus`, `baseline` and `acceptance_job`, which holds H1's job for H2 and N6.
  Its tests are marked `e2e` and `scripts/test.sh` leaves them out.
- Every direct read the suite makes is listed in the ladder's [direct reads](../../docs/acceptance-ladder.md#direct-reads).

## The smoke recipe

`just smoke` runs `scripts/smoke.sh`, which needs preset `ci` and `kind`, `kubectl`, `flock`, `timeout` and `uv` on `PATH`, takes `/tmp/dsy-cluster.lock` unless `DSY_CLUSTER_LOCK_HELD` is set, and runs the driver under a 300 s timer.

| Exit | Meaning |
| --- | --- |
| 0 | every row ok |
| 1 | a row failed, or the 300 s budget ran out (`FAIL budget`) |
| 2 | usage: a preset other than `ci`, a host tool missing, or `cluster-verify`'s own exit 2 passed through |
| 4 | refused: no cluster `dsy`, or one started for another preset, passed through from `cluster-verify`, or the lock not taken within its 900 s wait |

When the timer runs out, the driver gets SIGINT, prints `FAIL budget 300 s ran out in <row>` and its count line, and the wrapper exits 1.
A driver still running 10 s later is killed, and the wrapper prints `FAIL budget` and `smoke ci: budget ran out after 300 s` itself.

## The e2e recipe

`just e2e` runs `scripts/e2e.sh`, which needs preset `ci` with the observability stack on, takes the cluster lock as the smoke recipe does, checks that the kind cluster `dsy` exists, runs `scripts/smoke.sh` under the held lock, and then the suite under a 600 s timer.
Its exit codes are those of the smoke recipe, with exit 4 also meaning the stack is off.
A failed smoke ends the run with its exit, so the scenarios only run on a green gate.

## Commands

```sh
nix develop -c uv run --frozen pytest packages/acceptance -v
nix develop -c just smoke
```
