# RabbitMQ

The intake broker of the platform: one RabbitMQ node from the pinned `rabbitmq` image of `deploy/versions.yaml`, deployed in `wft-infra` on the infra pool.
Module: `rabbitmq`.
Its exchanges, queues, policies, users and memory figures are those of the [broker contract](../../contracts/broker/README.md), rendered by `wft_broker.topology` for the tenancy profile of the Tilt profile.
The sizing is in [the S4b spike](../../docs/delivery/spikes/S4b/broker-client-py313.md).

## Files

| File | Holds |
| --- | --- |
| `module.yaml` | the module manifest: the broker at 100m and 500m of CPU and 320 MiB of memory, requests equal to limits in memory |
| `Tiltfile.module` | loads the manifests that `config.py` renders, for the Tilt profile that the root `Tiltfile` exports as `WFT_TILT_PROFILE` |
| `config.py` | renders the ConfigMap, the Secret, the StatefulSet and the Service, and is the password helper of the broker users |
| `test_render.py` | the render tests, offline, one of them through `tilt alpha tiltfile-result` |

## What runs

| Object | Holds |
| --- | --- |
| StatefulSet `rabbitmq` | one pod `rabbitmq-0` with a 1 GiB volume at `/var/lib/rabbitmq`, the Erlang VM on one scheduler with busy waiting off, and a readiness probe on the AMQP port |
| Service `rabbitmq` | `rabbitmq.wft-infra.svc.cluster.local`, port 5672 for AMQP 1.0 and port 15692 for the Prometheus endpoints, `/metrics/detailed` among them |
| ConfigMap `rabbitmq` | `rabbitmq.conf`, `enabled_plugins` and the topology definitions, and no credential |
| Secret `rabbitmq` | the users definitions with salted password hashes, and the Erlang cookie |

The broker imports the topology and the users definitions from `/etc/rabbitmq/definitions` at every boot.
A definitions import adds and updates but never deletes, so a tenant or a class removed from a profile leaves its queue in place.
The pod template carries `wft.dev/config-digest`, a digest of the configuration, the passwords and the cookie, so a changed tenancy profile or a new password rolls the pod.
An init container copies the cookie onto the data volume, because the broker refuses a cookie file that is not its own and readable by it alone.
A StatefulSet does not replace a pod that never became ready, so after fixing a configuration that kept the broker from booting, delete `rabbitmq-0` and the new pod takes the fixed configuration.

## Credentials

The broker users are `dispatch`, `admission` and `operator`, with the permissions of the broker contract, and no `guest` user is created.
Their passwords and the Erlang cookie are generated on first use into the credentials directory `rabbitmq/` under the state directory of the cluster scripts (`$WFT_STATE_DIR`, by default `~/.local/state/wasm-dag-forge`).
It holds one file per user, `dispatch.password`, `admission.password` and `operator.password`, and `erlang.cookie`, each one line with no newline and readable by its owner only.
This is the directory that the regression suite and the dead-letter tool read with `--credentials-dir`.

A consumer module asks the helper for the password of its user and makes its own Secret in its own namespace from it:

```sh
uv run --frozen python deploy/rabbitmq/config.py password admission
```

The first call creates the password, whichever module loads first, and every later call returns the same one.
A deleted file is generated again, which rolls the broker on the next load.

## Reaching it from the host

There is no NodePort, so the host goes through a port-forward bound to 127.0.0.1:

```sh
kubectl --context kind-wft -n wft-infra port-forward --address 127.0.0.1 pod/rabbitmq-0 5672:5672 15692:15692
```

The queues, their policies and the users are listed inside the pod:

```sh
kubectl --context kind-wft -n wft-infra exec rabbitmq-0 -- rabbitmqctl list_queues name type policy messages
kubectl --context kind-wft -n wft-infra exec rabbitmq-0 -- rabbitmqctl list_users
```

## Changing the image

Set `version` and `digest` of `images.rabbitmq` in `deploy/versions.yaml`, then run `pytest deploy/rabbitmq -v`, the broker tests of `just ci`, and `scripts/tilt/run.sh ci` on a cluster.
