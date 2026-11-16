# wdf-broker

The models and the loader of the broker and tenancy contracts, the tenancy loader that the publisher and the consumer of the intake queues read, and the in-memory broker they test against at T0.
The contracts it loads are pinned in `contracts/broker/README.md` and `contracts/tenancy/README.md`.

## State

The models (`wdf_broker.models`), the loader `python -m wdf_broker.contract --check FILE [--as NAME]`, the schema generator `python -m wdf_broker.gen [--check]` and `wdf_broker.tenancy.load(profile)` are delivered.
`wdf_broker.contract` also returns the pinned files as typed models through `topology()`, `reasons()` and `tenancy()`.
`wdf_broker.models` holds the publish `Outcome` and the client errors `TopologyError`, `PublishTimeout` and `BrokerUnavailable`.
`wdf_broker.testing.FakeBroker` is an in-memory broker behind the client API, with the topology of a tenancy profile, credit, delivery counts, the delivery limit, `max-length` with `reject-publish`, `UNROUTABLE` for an unknown routing key, dead-lettering with the reason annotations, `restart()` and `alarm(on)`.
`wdf_broker.testing.broker_contract(connect)` is the conformance test that the fake passes and that the client must pass against a broker container, where `connect(user)` opens a connection as `dispatch`, `admission` or `operator`.
The client, the topology generator, the T1 broker container, the regression suite and the dead-letter tool are not built yet.

The client depends on `python-qpid-proton` 0.40.0, which publishes no Linux wheel, so `uv` builds it from its sdist with the `gcc` of the dev shell, and an image builds it in a build stage with `gcc` and `libc6-dev`.

## Package data

`wdf_broker/contract/` holds links to the contract files the package reads: `topology.yaml`, `reasons.yaml` and `tenancy.yaml`.
A built wheel carries the files themselves, so the package never reads a repository path.

## Commands

```sh
nix develop -c uv run --frozen pytest packages/broker -q
nix develop -c uv run --frozen pytest packages/broker -v -k loader
nix develop -c uv run --frozen pytest packages/broker -v -k fake_contract
nix develop -c uv run --frozen python -m wdf_broker.contract --check FILE
nix develop -c uv run --frozen python -m wdf_broker.gen --check
```
