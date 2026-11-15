# wdf-worker

The pool workers: one activity worker per pool (compile, simulate, aggregate) that serves `run_task` of the activity contract by running one child process at a time.
The contracts it serves are pinned in `contracts/activity/README.md` and `contracts/temporal/README.md`, and the worker detail is drawn in `docs/architecture/18-pool-worker.drawio`.

## State

Delivered:

- `wdf_worker.read_request`: the one place the worker reads its task request, held to the worker stage of `contracts/stages/stages.yaml`.
- `wdf_worker.config`: the configuration of `python -m wdf_worker --pool <pool>`, read from the environment only, which exits 2 on a missing or malformed variable.
- `wdf_worker.pools.Context`: what a task path runs with.
- `wdf_worker.testing.contract_cases`: a succeeding and a failing task input per pool over a seeded store, for `wdf_workflow.testing.activity_contract`, with the class each failing input fails with in `FAILS`.
  It builds the workload library, so it runs in the repository's dev environment only.

Stubbed, each listed in its module's `STUBBED` and raising `NotImplementedError("not implemented: owned by lane <letter>")`:

- `wdf_worker.runner`: `Child`, the process group runner.
- `wdf_worker.pools.{compile,simulate,aggregate}`: `run`, the task path of each pool.
- `wdf_worker.activity`: `run_task`.
- `wdf_worker.service`, `wdf_worker.__main__` and `wdf_worker.probe`: the service, its command line and the egress probe.

## Extras

Each pool image installs one extra beside the core: `compile` adds `wdf-compile` and `uv`, `simulate` adds `wdf-runtime`, and `aggregate` adds `wdf-aggregate`.
The core depends on `wdf-workflow` for the activity models, so every image also carries its chain.

## Commands

```sh
nix develop -c uv run --frozen pytest packages/worker -q -rs
nix develop -c uv run --frozen pytest packages/worker -v -m integration -k contract_cases
nix develop -c uv export --frozen --package wdf-worker --extra compile
```
