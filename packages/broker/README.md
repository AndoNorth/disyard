# wdf-broker

The models and the loader of the broker and tenancy contracts, and the tenancy loader that the publisher and the consumer of the intake queues read.
The contracts it loads are pinned in `contracts/broker/README.md` and `contracts/tenancy/README.md`.

## State

The models (`wdf_broker.models`), the loader `python -m wdf_broker.contract --check FILE [--as NAME]`, the schema generator `python -m wdf_broker.gen [--check]` and `wdf_broker.tenancy.load(profile)` are delivered.
`wdf_broker.contract` also returns the pinned files as typed models through `topology()`, `reasons()` and `tenancy()`.
The client, the fake broker, the topology generator, the regression suite and the dead-letter tool are not built yet.

## Package data

`wdf_broker/contract/` holds links to the contract files the package reads: `topology.yaml`, `reasons.yaml` and `tenancy.yaml`.
A built wheel carries the files themselves, so the package never reads a repository path.

## Commands

```sh
nix develop -c uv run --frozen pytest packages/broker -q
nix develop -c uv run --frozen pytest packages/broker -v -k loader
nix develop -c uv run --frozen python -m wdf_broker.contract --check FILE
nix develop -c uv run --frozen python -m wdf_broker.gen --check
```
