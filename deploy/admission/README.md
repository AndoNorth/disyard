# Admission

The admission service, `python -m wft_admission.service`, deployed in `wft-control` on the infra pool.
One process decides when each submitted job may start, claims its row and starts its workflow, within the caps of the tenancy profile, as [the package README](../../packages/admission/README.md) says.
Module: `admission`.

## Files

| File | Holds |
| --- | --- |
| `module.yaml` | the module manifest: the pod at 50m and 250m of CPU and 128 MiB of memory, requests equal to limits in memory, and the nominal entry of the login resource |
| `Tiltfile.module` | the image build, the two Secrets, the login resource and the Deployment |
| `image.sh`, `Dockerfile` | the image: wheels of the workspace packages built on the host, over the third-party packages that `uv.lock` pins by hash, on the pinned `python` image of `deploy/versions.yaml` |
| `logins.sh`, `logins.py` | the service login in the app database, whose password reaches the server as a SCRAM verifier, and the render of its Secret |
| `test_render.py`, `test_logins.py` | the render tests, offline through `tilt alpha tiltfile-result`, and the integration tests of the login against a migrated database |

## Image

`python-qpid-proton`, the AMQP library of the broker client, publishes no Linux wheel, so the `Dockerfile` builds one from its sdist in a stage of its own.
That stage installs `gcc`, `libc6-dev` and `setuptools` from the network, then, with no network, checks the sdist against the hash that `uv.lock` pins and builds it without build isolation over the hash-pinned `cffi`.
The service image takes only the built wheel and the other hash-pinned packages, and has no compiler.

To build the image by hand:

```sh
deploy/admission/image.sh <ref>
```

## Configuration

The service reads only its environment.

| Variable | From |
| --- | --- |
| `WFT_TEMPORAL_ADDRESS`, `WFT_TEMPORAL_NAMESPACE` | the Temporal frontend in `wft-infra` and the namespace of `contracts/temporal/naming.yaml` |
| `WFT_BROKER_ADDRESS` | `rabbitmq.wft-infra.svc.cluster.local:5672` |
| `WFT_TENANCY_PROFILE` | the tenancy profile that `contracts/tenancy/tenancy.yaml` maps the Tilt profile to |
| `WFT_METRICS_ADDRESS` | port 9464 of the pod, the Prometheus endpoint of the admission metrics |
| `WFT_READY_FILE` | `/tmp/wft-ready` |
| `WFT_ADMISSION_DSN` | the Secret `admission-logins` |
| `WFT_BROKER_PASSWORD` | the Secret `admission-broker` |

The module sets neither `WFT_ADMISSION_GLOBAL_CAP`, which can only lower the profile's global cap, nor `WFT_POOL_QUEUE_PREFIX`, which puts the pool queues of every job it starts behind a prefix.
Only the admission driver sets them, for its run, and removes them after.
A driver run killed with no cleanup at all leaves them set until the next driver run, or until they are removed by hand:

```sh
kubectl --context kind-wft -n wft-control set env deploy/admission WFT_POOL_QUEUE_PREFIX- WFT_ADMISSION_GLOBAL_CAP-
```

## Logins and Secrets

The group role `wft_admission` cannot log in, so the service has the login `admission` in it.
Its password is generated on first use into `admission.logins.json` in the state directory of the cluster scripts, readable by its owner only, and never committed.
The Secret `admission-logins` holds its DSN as `WFT_ADMISSION_DSN`.

Tilt runs `logins.sh apply` as the resource `admission-logins` once the migrations are applied, and the service starts after it.
It creates a missing login, and converges one whose password, attributes or membership differs: a login that inherits, has no superuser, role, database, replication or row-security bypass right and no expiry, and is a member of `wft_admission` only, with no admin option.
A second run prints the login `unchanged` and `changed: 0`.
`logins.sh check` exits 1 when the login differs or cannot log in with its password.

The Secret `admission-broker` holds the password of the broker user `admission` as `WFT_BROKER_PASSWORD`, taken from the broker module's helper, which generates it on first use:

```sh
uv run --frozen python deploy/rabbitmq/config.py password admission
```

## Readiness

The service creates `/tmp/wft-ready` once its cycle runs, and removes it when it starts to drain.
The readiness probe tests for the file, and the module is ready when the Deployment `admission` is `Available`.
The Deployment uses the `Recreate` strategy, because one process admits, with no leader election.
The log shows the queues it polls:

```sh
kubectl --context kind-wft -n wft-control logs deploy/admission
```

## Changing the base image

Set `version` and `digest` of `images.python` in `deploy/versions.yaml`, keeping the Python minor of `requires-python`, then run `pytest deploy/admission -v` and `scripts/tilt/run.sh ci` on a cluster.
