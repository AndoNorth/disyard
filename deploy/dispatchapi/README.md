# Dispatch

The dispatch and control API, `python -m dsy_dispatchapi.service`, deployed in `dsy-control` on the infra pool.
One process serves two listeners, `dispatch-api` for uploads, batch submit and batch reads and `control-api` for control and the job status read, and runs the reconciler and the control retrier, as [the API contract](../../contracts/api/README.md) says.
Module: `dispatch`, the name of the reserve row that the cluster budget held for it.

## Files

| File | Holds |
| --- | --- |
| `module.yaml` | the module manifest: the pod at 100m and 500m of CPU and 256 MiB of memory, requests equal to limits in memory, and the nominal entry of the login resource |
| `Tiltfile.module` | the image build, the three Secrets, the login resource, the Deployment and the two NodePort Services |
| `image.sh`, `Dockerfile` | the image: wheels of the workspace packages built on the host, over the third-party packages that `uv.lock` pins by hash, on the pinned `python` image of `deploy/versions.yaml` |
| `logins.sh`, `logins.py` | the service logins in the app database, whose passwords reach the server as SCRAM verifiers, and the render of the Secrets of the logins and the store identity |
| `test_render.py`, `test_logins.py` | the render tests, offline through `tilt alpha tiltfile-result`, and the integration tests of the logins against a migrated database |

## Image

`python-qpid-proton`, the AMQP library of the broker client, publishes no Linux wheel, so the `Dockerfile` builds one from its sdist in a stage of its own.
That stage installs `gcc`, `libc6-dev` and `setuptools` from the network, then, with no network, checks the sdist against the hash that `uv.lock` pins and builds it without build isolation over the hash-pinned `cffi`.
The service image takes only the built wheel and the other hash-pinned packages, and has no compiler.

To build the image by hand:

```sh
deploy/dispatchapi/image.sh <ref>
```

## Ports

| Listener | Container port | Service | NodePort | Host |
| --- | --- | --- | --- | --- |
| `dispatch-api` | 8081 | `dispatch-api` | 30081 | `127.0.0.1:18081` |
| `control-api` | 8080 | `control-api` | 30080 | `127.0.0.1:18080` |

The NodePorts are those of the rows of the [host access map](../host/ports.yaml).
The Prometheus metrics are on container port 9090, which has no Service:

```sh
kubectl --context kind-dsy -n dsy-control port-forward --address 127.0.0.1 deploy/dispatch 9090:9090
```

## Configuration

The service reads only its environment.

| Variable | From |
| --- | --- |
| `DSY_TEMPORAL_ADDRESS` | the Temporal frontend in `dsy-infra` |
| `DSY_STORE_ENDPOINT` | the object store in `dsy-infra` |
| `DSY_SUBMIT_CREDENTIALS` | `/etc/dsy/store/credentials.json`, mounted from the Secret `dispatch-store` |
| `DSY_BROKER_ADDRESS` | `rabbitmq.dsy-infra.svc.cluster.local:5672` |
| `DSY_TENANCY_PROFILE` | the tenancy profile that `contracts/tenancy/tenancy.yaml` maps the Tilt profile to |
| `DSY_METRICS_ADDRESS` | port 9090 of the pod |
| `DSY_DISPATCH_DSN`, `DSY_CONTROL_DSN`, `DSY_ARTIFACTS_DSN`, `DSY_READER_DSN` | the Secret `dispatch-logins` |
| `DSY_BROKER_PASSWORD` | the Secret `dispatch-broker` |

The listeners take their container ports, because the module sets neither `DSY_DISPATCH_API_LISTEN` nor `DSY_CONTROL_API_LISTEN`.
The submit limits keep their defaults, each of which a `DSY_DISPATCH_<NAME>` variable can replace without a rebuild.

## Logins and Secrets

The group roles of the app database cannot log in, so the service has one login in each group it writes or reads as.

| Login | Group role | Used by | DSN variable |
| --- | --- | --- | --- |
| `dispatch_dispatch` | `dsy_dispatch` | the submit sequence, the publisher and the reconciler | `DSY_DISPATCH_DSN` |
| `dispatch_control` | `dsy_control` | control, the retrier and the resume finisher | `DSY_CONTROL_DSN` |
| `dispatch_artifacts` | `dsy_artifacts` | the index and link rows of a submit and of a patched resume | `DSY_ARTIFACTS_DSN` |
| `dispatch_reader` | `dsy_reader` | the status read and the reasons that the retrier signals | `DSY_READER_DSN` |

Their passwords are generated on first use into `dispatch.logins.json` in the state directory of the cluster scripts, readable by its owner only, and never committed.
The Secret `dispatch-logins` holds the four DSNs, and the Secret `dispatch-store` holds `credentials.json` with the `submit` store identity only, taken from `objectstore.credentials.json`.

Tilt runs `logins.sh apply` as the resource `dispatch-logins` once the migrations are applied, and the service starts after it.
It creates a missing login, and converges one whose password, attributes or membership differs: a login that inherits, has no superuser, role, database, replication or row-security bypass right and no expiry, and is a member of its group role only, with no admin option.
A second run prints every login `unchanged` and `changed: 0`.
`logins.sh check` exits 1 when a login differs or cannot log in with its password.

The Secret `dispatch-broker` holds the password of the broker user `dispatch` as `DSY_BROKER_PASSWORD`, taken from the broker module's helper, which generates it on first use:

```sh
uv run --frozen python deploy/rabbitmq/config.py password dispatch
```

## Readiness

The readiness probe asks `GET /readyz` of `dispatch-api`, which answers ready while every login connects, the store answers a head and Temporal answers, and not-ready once the service starts to stop.
The module is ready when the Deployment `dispatch` is `Available`.
The Deployment uses the `Recreate` strategy, because one process runs the reconciler and the retrier, with no leader election.
On SIGTERM the service finishes the chunk of a submit in hand within its drain of 25 s, below the grace period of 30 s, and answers an `ErrorLine`, after which a resend of the whole call is safe.

```sh
curl -s http://127.0.0.1:18081/readyz
curl -s http://127.0.0.1:18080/readyz
kubectl --context kind-dsy -n dsy-control logs deploy/dispatch
```

## Smoke driver

`python -m dsy_dispatchapi.smoke --scenario NAME` drives the deployed API from the host, from the root of the tree, through the two host ports, and through port-forwards for what it counts beyond the API.
Its scenarios are `upload`, `submit`, `cancel-before-start`, `run`, `pause-resume`, `cancel-running` and `restart`, and each prints the lines its module docstring names.
`cancel-before-start` scales `admission` to no replica while it submits and cancels its job, and back after.
`restart` deletes the `dispatch` pod during a batch call, sends the call again, and cancels the batch once it has counted.
Its 2000 jobs fill the global cap of 1000 of the `ci` tenancy profile and the intake queue's length of 1000 exactly, so it expects `unpublished: 0` only on a cluster with no job left at `admitted` or `running`.
Every scenario but `upload` first makes the deployed workflow worker build current when no build is, as the workflow harness does, since a fresh cluster has none and gives a new job to no worker.
The jobs of `run`, `pause-resume`, `cancel-running` and `restart` run on the deployed pool workers.

```sh
uv run --frozen python -m dsy_dispatchapi.smoke --scenario submit --jobs 3
```

## Changing the base image

Set `version` and `digest` of `images.python` in `deploy/versions.yaml`, keeping the Python minor of `requires-python`, then run `pytest deploy/dispatchapi -v` and `scripts/tilt/run.sh ci` on a cluster.
