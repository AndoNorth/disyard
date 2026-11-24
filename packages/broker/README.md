# dsy-broker

The broker client that the publisher and the consumer of the intake queues use, the models and the loader of the broker and tenancy contracts, the topology generator of the broker configuration, the regression suite and the dead-letter tool of the operator, and the brokers they test against: in memory at T0 and in a container at T1.
The contracts it loads are pinned in `contracts/broker/README.md` and `contracts/tenancy/README.md`.

## State

The models (`dsy_broker.models`), the loader `python -m dsy_broker.contract --check FILE [--as NAME]`, the schema generator `python -m dsy_broker.gen [--check]` and `dsy_broker.tenancy.load(profile)` are delivered.
`dsy_broker.contract` also returns the pinned files as typed models through `topology()`, `reasons()` and `tenancy()`.
`dsy_broker.models` holds the publish `Outcome` and the client errors `TopologyError`, `PublishTimeout` and `BrokerUnavailable`.
`dsy_broker.testing.FakeBroker` is an in-memory broker behind the client API, with the topology of a tenancy profile, credit, delivery counts, the delivery limit, `max-length` with `reject-publish`, `UNROUTABLE` for an unknown routing key, dead-lettering with the reason annotations, `restart()` and `alarm(on)`.
`dsy_broker.testing.broker_contract(connect)` is the conformance test that the fake passes and that the client passes against a broker container, where `connect(user)` opens a connection as `dispatch`, `admission` or `operator`.
`dsy_broker.client.connect(address, Credentials(user, password))` opens a connection with `publish`, `consumer(queue)` and `browser(queue)`, and a consumer hands out deliveries through `pull` and `drain` that the caller settles with `ack`, `retry`, `dead_letter` or `release`.
`python -m dsy_broker.topology --profile NAME [--users FILE] [--out DIR]` renders the broker configuration for a tenancy profile, the files that the `rabbitmq` module deploys (`deploy/rabbitmq/README.md`).
`dsy_broker.testing.container.broker_container(t1, profile)` starts the pinned RabbitMQ image with that configuration in a T1 session and yields a `Broker` with its address, metrics endpoint and user passwords.
`python -m dsy_broker.suite --address HOST:PORT --metrics URL --credentials-dir DIR COMMAND` is the regression suite, where `check` runs every check and `fill`, `drain`, `count` and `deadletter` stand in for the publisher and the consumer.
`python -m dsy_broker.dlq {list,show,replay,purge} --address HOST:PORT --credentials-dir DIR` is the dead-letter tool of the operator.

The client depends on `python-qpid-proton` 0.40.0, which publishes no Linux wheel, so `uv` builds it from its sdist with the `gcc` of the dev shell, and an image builds it in a build stage with `gcc` and `libc6-dev`.

## Package data

`dsy_broker/contract/` holds links to the contract files the package reads: `topology.yaml`, `reasons.yaml` and `tenancy.yaml`.
A built wheel carries the files themselves, so the package never reads a repository path.

## Commands

```sh
nix develop -c uv run --frozen pytest packages/broker -q
nix develop -c uv run --frozen pytest packages/broker -v -k loader
nix develop -c uv run --frozen pytest packages/broker -v -k fake_contract
nix develop -c uv run --frozen python -m dsy_broker.contract --check FILE
nix develop -c uv run --frozen python -m dsy_broker.gen --check
```
