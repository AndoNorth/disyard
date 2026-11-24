# dsy-admission

Job admission, layer 1 scheduling: it decides when a submitted job may start, claims its row from `accepted` to `admitted` and starts its workflow, within the caps of the tenancy profile.
Jobs wait in the intake queues and the job table, not in Temporal, until admitted.
The caps, quotas, class order and reserve interval are read from `contracts/tenancy/tenancy.yaml` through `dsy_broker.tenancy`, and the start follows `contracts/temporal/naming.yaml`.
The admission cycle is drawn in `docs/architecture/17-admission-cycle.drawio`, and its place between intake and the workflow in `docs/architecture/10-intake-and-admission.drawio`.

## Commands

```sh
nix develop -c uv run --frozen pytest packages/admission -q -rs
nix develop -c uv run --frozen pytest packages/admission -v -k wheel
```
