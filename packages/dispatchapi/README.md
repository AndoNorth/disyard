# wdf-dispatchapi

The dispatch and control API: a submitter sends a batch of jobs and gets an answer per job, and an operator cancels, pauses, resumes or terminates a job.
The API writes the job row first and holds no job state of its own: Postgres owns the control record and Temporal owns lifecycle.
Its routes, bodies, reason codes, control table and fingerprint are pinned in [contracts/api/](../../contracts/api/README.md), whose schemas are generated from `wdf_dispatchapi.models`.
The submit and control paths are drawn in [20-dispatch-and-control.drawio](../../docs/architecture/20-dispatch-and-control.drawio).

## Workflow gateway

Control and the reads reach a job's workflow only through the `WorkflowGateway` port of `wdf_dispatchapi.ports`, which sends `cancel`, `terminate` and `pause` and never `resume`, which admission alone sends.
`wdf_dispatchapi.testing` holds its stub `FakeGateway` for tests without Temporal, and `gateway_contract(gateway, job_id)`, the conformance test that the stub and the Temporal gateway both pass.

## Commands

```sh
nix develop -c uv run --frozen python -m wdf_dispatchapi.contract --check contracts/api/control.yaml
nix develop -c uv run --frozen python -m wdf_dispatchapi.gen --check
nix develop -c uv run --frozen pytest packages/dispatchapi -q -rs
```
