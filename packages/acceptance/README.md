# wft-acceptance

Acceptance of a deployed cluster from the host, imported as `wft_acceptance`.
It holds the smoke gate and the small clients it drives the platform through, and the e2e suite is written over the same clients.
It builds on `wft_clusterkit` for every cluster call: forwards, kubectl, readiness, polling and credentials.

It is test tooling, so no service depends on it and no image installs it.
The dev group of the workspace installs it.

## Dependencies

`pyproject.toml` declares every dependency the clients and the gate import: `temporalio`, `wft-broker`, `wft-clusterkit`, `wft-kernel`, `wft-workflow` and `wft-workloads`.
`tests/test_wheel.py` builds the wheel, installs it with only those dependencies into a clean virtual environment and imports every module of the package, so a module that imports an undeclared dependency fails there.

## State

`wft_acceptance.report` is delivered: the row line, the count line and the exit codes of the smoke recipe contract.

- `ok(name, detail)`, `fail(name, detail)` and `skip(name, reason)` return a `Row`, whose `line` is `ok|FAIL|skip <name> <detail>`, the format of `cluster-verify`.
- `count_line(preset, rows, elapsed_s)` is `smoke <preset>: <n> ok, <n> failed, <n> skipped, <s> s`, and every row counts in exactly one bucket.
- `exit_code(rows, passed_through=None)` is 0 when every row is ok and 1 otherwise, or `cluster-verify`'s usage exit 2 or refusal exit 4 when one of those ended the run.

Not written yet, with the shape the gate and the e2e suite build on:

- `wft_acceptance.clients.api`: `template`, `batch`, and `Api(dispatch, control, tenant)` with `upload_source`, `submit_one`, `status` and `cancel_batch`.
- `wft_acceptance.clients.store`: `fetch(uri, identity) -> bytes`.
- `wft_acceptance.clients.temporal`: `connect(address) -> Client`.
- `wft_acceptance.clients.broker`: `dead_letters(address) -> int`.
- `python -m wft_acceptance.smoke`: the ordered driver of the gate, run by `scripts/smoke.sh`.

## The smoke recipe

`just smoke` runs `scripts/smoke.sh`, which needs preset `ci` and `kind`, `kubectl`, `flock`, `timeout` and `uv` on `PATH`, takes `/tmp/wft-cluster.lock` unless `WFT_CLUSTER_LOCK_HELD` is set, and runs the driver under a 300 s timer.

| Exit | Meaning |
| --- | --- |
| 0 | every row ok |
| 1 | a row failed, or the 300 s budget ran out (`FAIL budget`) |
| 2 | usage: a preset other than `ci`, a host tool missing, or `cluster-verify`'s own exit 2 passed through |
| 4 | refused: no cluster `wft`, or one started for another preset, passed through from `cluster-verify`, or the lock not taken within its 900 s wait |

When the timer runs out, the driver gets SIGINT, prints `FAIL budget 300 s ran out in <row>` and its count line, and the wrapper exits 1.
A driver still running 10 s later is killed, and the wrapper prints `FAIL budget` and `smoke ci: budget ran out after 300 s` itself.

## Commands

```sh
nix develop -c uv run --frozen pytest packages/acceptance -v
nix develop -c just smoke
```
