# wdf-dispatchapi

The dispatch and control API: a submitter sends a batch of jobs and gets an answer per job, and an operator cancels, pauses, resumes or terminates a job.
The API writes the job row first and holds no job state of its own: Postgres owns the control record and Temporal owns lifecycle.
Its routes, bodies, reason codes, control table and fingerprint are pinned in [contracts/api/](../../contracts/api/README.md), whose schemas are generated from `wdf_dispatchapi.models`.

## Commands

```sh
nix develop -c uv run --frozen python -m wdf_dispatchapi.contract --check contracts/api/control.yaml
nix develop -c uv run --frozen python -m wdf_dispatchapi.gen --check
nix develop -c uv run --frozen pytest packages/dispatchapi -q -rs
```
