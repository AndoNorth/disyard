"""The manifests of the broker module, and the password of each broker user.

The broker configuration is rendered by `wft_broker.topology` from the broker contract and the
tenancy profile that the Tilt profile maps to. The passwords and the Erlang cookie are generated on
first use into `rabbitmq/` under the state directory of the cluster scripts, one file per user named
`<user>.password` and `erlang.cookie`, readable by their owner only and never committed.
Usage: config.py render TILT_PROFILE | password USER
`render` prints the ConfigMap, the Secret, the StatefulSet and the Service of the module as a JSON
list, and `password` prints the password of one broker user, so that a consumer module can make its
own Secret from it.
Exit 0 done, 2 usage.
"""

import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

import yaml
from wft_broker import topology
from wft_broker.contract import tenancy
from wft_broker.contract import topology as spec
from wft_broker.tenancy import load

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
COOKIE = "erlang.cookie"
USERS = Path(topology.USERS).name
DEFINITIONS = Path(topology.TOPOLOGY).name
DATA = "/var/lib/rabbitmq"
# The uid and gid of the rabbitmq user of the image, which the entrypoint switches to.
RABBITMQ_ID = 999


def credentials_dir() -> Path:
    state = os.environ.get("WFT_STATE_DIR") or str(
        Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "weft"
    )
    return Path(state) / "rabbitmq"


def _generated(name: str) -> str:
    """The secret held in the file `name` of the credentials directory, generated on first use.

    The first writer wins, so two processes that start at once read the same value.
    """
    path = credentials_dir() / name
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(f".{name}.{os.getpid()}")
        tmp.touch(mode=0o600)
        tmp.write_text(secrets.token_hex(24))
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
        finally:
            tmp.unlink()
    return path.read_text()


def users() -> list[str]:
    return list(spec().users.model_dump())


def password(user: str) -> str:
    """The password of a broker user. Raises KeyError for a user the contract does not name."""
    if user not in users():
        raise KeyError(f"unknown broker user {user!r}, the users are {', '.join(users())}")
    return _generated(f"{user}.password")


def _read_yaml(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text())


def render(tilt_profile: str, profile=None) -> list[dict]:
    """The manifests of the module for a Tilt profile, rendered from the tenancy profile it maps to,
    or from `profile` when given.

    Raises KeyError for a Tilt profile the tenancy contract does not map.
    """
    module = _read_yaml("deploy/rabbitmq/module.yaml")
    name, namespace = module["name"], module["namespace"]
    pin = _read_yaml("deploy/versions.yaml")["images"][module["artifact"]["pin"]]
    cluster = _read_yaml("contracts/topology/topology.yaml")
    mapping = tenancy().tilt_profiles
    if tilt_profile not in mapping:
        raise KeyError(f"no tenancy profile for Tilt profile {tilt_profile!r}")
    profile_name = mapping[tilt_profile]
    if profile is None:
        profile = load(profile_name)
    ports = spec().ports
    files = topology.render(profile, {user: password(user) for user in users()})
    config = {
        topology.CONF: files[topology.CONF],
        topology.PLUGINS: files[topology.PLUGINS],
        DEFINITIONS: files[topology.TOPOLOGY],
    }
    cookie = _generated(COOKIE)
    # The users definitions carry a fresh salt on every render, so the digest takes the passwords.
    digest = hashlib.sha256(
        json.dumps([config, [password(u) for u in users()], cookie]).encode()
    ).hexdigest()
    labels = {"app": name}
    pool = {"key": cluster["pool_label"], "value": "infra"}
    resources = module["resources"][name]
    image = "docker.io/library/rabbitmq@" + pin["digest"]
    meta = {"name": name, "namespace": namespace}
    return [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta, "data": config},
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": meta,
            "type": "Opaque",
            "stringData": {USERS: files[topology.USERS], COOKIE: cookie},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": meta,
            "spec": {
                "serviceName": name,
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {
                        "labels": labels,
                        # A changed configuration or secret rolls the pod, because the broker reads
                        # both only at boot.
                        "annotations": {
                            "wft.dev/config-digest": digest,
                            "wft.dev/tenancy-profile": profile_name,
                        },
                    },
                    "spec": {
                        "nodeSelector": {pool["key"]: pool["value"]},
                        "tolerations": [
                            {
                                "key": pool["key"],
                                "operator": "Equal",
                                "value": pool["value"],
                                "effect": cluster["taint_effect"],
                            }
                        ],
                        "automountServiceAccountToken": False,
                        # Lets the rabbitmq user read the users definitions, owned by root.
                        "securityContext": {"fsGroup": RABBITMQ_ID},
                        # The cookie must be a file of the rabbitmq user readable by it alone,
                        # which a Secret mount cannot give, so it is copied onto the data volume.
                        "initContainers": [
                            {
                                "name": "cookie",
                                "image": image,
                                "command": [
                                    "sh",
                                    "-c",
                                    f"install -m 0400 -o {RABBITMQ_ID} -g {RABBITMQ_ID}"
                                    f" /cookie/{COOKIE} {DATA}/.erlang.cookie",
                                ],
                                "resources": resources,
                                "volumeMounts": [
                                    {"name": "data", "mountPath": DATA},
                                    {"name": "cookie", "mountPath": "/cookie", "readOnly": True},
                                ],
                            }
                        ],
                        "containers": [
                            {
                                "name": name,
                                "image": image,
                                "env": [
                                    {
                                        "name": "RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS",
                                        "value": topology.ERL_ARGS,
                                    }
                                ],
                                "ports": [
                                    {"name": "amqp", "containerPort": ports.amqp},
                                    {"name": "prometheus", "containerPort": ports.prometheus},
                                ],
                                # The definitions are imported before the listeners open.
                                "readinessProbe": {
                                    "tcpSocket": {"port": "amqp"},
                                    "periodSeconds": 5,
                                },
                                "resources": resources,
                                "volumeMounts": [
                                    {"name": "data", "mountPath": DATA},
                                    {
                                        "name": "config",
                                        "mountPath": f"/etc/rabbitmq/{topology.CONF}",
                                        "subPath": topology.CONF,
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "config",
                                        "mountPath": f"/etc/rabbitmq/{topology.PLUGINS}",
                                        "subPath": topology.PLUGINS,
                                        "readOnly": True,
                                    },
                                    # The broker imports every entry of the directory, so each
                                    # file is mounted alone, without the volume's own entries.
                                    *(
                                        {
                                            "name": "definitions",
                                            "mountPath": f"{topology.DEFINITIONS_PATH}/{file}",
                                            "subPath": file,
                                            "readOnly": True,
                                        }
                                        for file in (DEFINITIONS, USERS)
                                    ),
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "config", "configMap": {"name": name}},
                            {
                                "name": "cookie",
                                "secret": {
                                    "secretName": name,
                                    "items": [{"key": COOKIE, "path": COOKIE}],
                                    "defaultMode": 0o400,
                                },
                            },
                            {
                                "name": "definitions",
                                "projected": {
                                    "defaultMode": 0o440,
                                    "sources": [
                                        {
                                            "configMap": {
                                                "name": name,
                                                "items": [
                                                    {"key": DEFINITIONS, "path": DEFINITIONS}
                                                ],
                                            }
                                        },
                                        {
                                            "secret": {
                                                "name": name,
                                                "items": [{"key": USERS, "path": USERS}],
                                            }
                                        },
                                    ],
                                },
                            },
                        ],
                    },
                },
                "volumeClaimTemplates": [
                    {
                        "metadata": {"name": "data"},
                        "spec": {
                            "accessModes": ["ReadWriteOnce"],
                            "resources": {"requests": {"storage": "1Gi"}},
                        },
                    }
                ],
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": meta,
            "spec": {
                "clusterIP": "None",
                "selector": labels,
                "ports": [
                    {"name": "amqp", "port": ports.amqp, "targetPort": ports.amqp},
                    {
                        "name": "prometheus",
                        "port": ports.prometheus,
                        "targetPort": ports.prometheus,
                    },
                ],
            },
        },
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("render", "password"):
        print("usage: config.py render TILT_PROFILE | password USER", file=sys.stderr)
        return 2
    try:
        if argv[0] == "render":
            print(json.dumps(render(argv[1])))
        else:
            print(password(argv[1]))
    except KeyError as error:
        print(f"usage: {error.args[0]}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
