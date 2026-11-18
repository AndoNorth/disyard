# wdf-placement

Task placement, layer 2 scheduling: for each ready task it picks the pool task queue, the memory class and the activity timeouts, and nothing else.
The workflow's `place` local activity asks it, and the answer is kept in the workflow history, so the service holds no state and no backlog.
The verdict comes from the pool table, `contracts/temporal/pools.yaml`, through the `wdf_workflow.placement` models and `StaticPlacer`.
A read-only snapshot of the pool nodes and pods, read on a slow cadence, is recorded beside each answer and never waited on.
The service and its neighbours are drawn in `docs/architecture/11-placement-and-workers.drawio`.

## Commands

```sh
nix develop -c uv run --frozen pytest packages/placement -q -rs
nix develop -c uv run --frozen pytest packages/placement -v -k wheel
```
