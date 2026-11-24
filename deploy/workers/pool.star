# The module of one pool worker, loaded by deploy/workers/<pool>/Tiltfile.module: the pool's image, one Deployment
# <pool>-worker of one single-task pod on the pool's node, the Secret of the pool's own store identity, and the
# NetworkPolicy that limits the pod's egress to the platform endpoints the pool needs.
# The queue is unversioned and no build id reaches the pod, so a pod serves the workflows of every build.
# Relative paths resolve against the calling module's directory.

load("../tilt/obs.star", "obs_env")

# The scratch volume and what lives on it: task scratch, the wasmtime module cache, HOME, the caches and the ready file.
SCRATCH = "/var/dsy"
SCRATCH_ENV = {
    "HOME": SCRATCH + "/home",
    "TMPDIR": SCRATCH + "/tmp",
    "XDG_CACHE_HOME": SCRATCH + "/cache",
    "UV_CACHE_DIR": SCRATCH + "/cache/uv",
}
READY = SCRATCH + "/ready"
CREDENTIALS = "/etc/dsy/store/credentials.json"
# The user every pool image runs as.
USER = 65532
MIRROR = "http://mirror.dsy-infra.svc.cluster.local"

# The pods each pool may reach, as namespace, pod labels and target port. Every pool needs DNS, the Temporal frontend
# and the object store, and the compile pool also the package mirror for its dependency fetch.
DNS = ("kube-system", {"k8s-app": "kube-dns"}, 53)
TEMPORAL = ("dsy-infra", {"app.kubernetes.io/name": "temporal", "app.kubernetes.io/component": "frontend"}, 7233)
STORE = ("dsy-infra", {"app": "objectstore"}, 8333)
MIRROR_POD = ("dsy-infra", {"app": "mirror"}, 8080)

# The SDK metrics port, named `metrics` so the stack's pod scrape finds it, and the stack pods a pool reaches with the
# observability switch on: Prometheus scrapes the port, and the pod pushes to the collector and Pyroscope.
METRICS_PORT = 9464
PROMETHEUS = ("dsy-obs", {"app.kubernetes.io/name": "prometheus"}, METRICS_PORT)
COLLECTOR = ("dsy-obs", {"app.kubernetes.io/name": "collector"}, 4318)
PYROSCOPE = ("dsy-obs", {"app.kubernetes.io/name": "pyroscope"}, 4040)

def _svc(service, port):
    return "%s.dsy-infra.svc.cluster.local:%d" % (service, port)

def _egress(peer, protocols = ["TCP"]):
    namespace, labels, port = peer
    return {
        "to": [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
            "podSelector": {"matchLabels": labels},
        }],
        "ports": [{"protocol": p, "port": port} for p in protocols],
    }

def _ingress(peer):
    namespace, labels, port = peer
    return {
        "from": [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
            "podSelector": {"matchLabels": labels},
        }],
        "ports": [{"protocol": "TCP", "port": port}],
    }

def pool_worker(pool):
    module = read_yaml("module.yaml")
    name = module["name"]
    namespace = module["namespace"]
    res = module["resources"][name]
    topology = read_yaml("../../../contracts/topology/topology.yaml")
    naming = read_yaml("../../../contracts/temporal/naming.yaml")
    labels = {"app.kubernetes.io/name": name}

    image = "dsy-" + name
    custom_build(
        image,
        [os.path.abspath("../image.sh"), pool],
        deps = ["../../../packages", "../../../uv.lock", "../../../nix/wasm-tools.nix", "../Dockerfile", "../image.sh", "../../versions.yaml"],
    )

    store = str(local([os.path.abspath("../../../scripts/storage/store/identities.sh"), "path"], quiet = True)).strip()
    k8s_yaml(encode_yaml({
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name + "-store", "namespace": namespace},
        "type": "Opaque",
        # The store role of each pool is named after the pool.
        "stringData": {"credentials.json": encode_json({pool: read_json(store)[pool]})},
    }))

    egress = [_egress(DNS, ["UDP", "TCP"]), _egress(TEMPORAL), _egress(STORE)]
    env = {
        "DSY_TEMPORAL_ADDRESS": _svc("temporal-frontend", TEMPORAL[2]),
        "DSY_NAMESPACE": naming["namespace"],
        "DSY_STORE_ENDPOINT": "http://" + _svc("objectstore", STORE[2]),
        "DSY_STORE_CREDENTIALS": CREDENTIALS,
        "DSY_SCRATCH": SCRATCH,
        "DSY_READY_FILE": READY,
        "DSY_METRICS_ADDRESS": "0.0.0.0:%d" % METRICS_PORT,
    }
    env.update(SCRATCH_ENV)
    obs = obs_env()
    env.update(obs)
    policy = {
        "podSelector": {"matchLabels": labels},
        "policyTypes": ["Ingress", "Egress"],
        "egress": egress,
    }
    if obs:
        egress.extend([_egress(COLLECTOR), _egress(PYROSCOPE)])
        policy["ingress"] = [_ingress(PROMETHEUS)]
    if pool == "compile":
        egress.append(_egress(MIRROR_POD))
        env["DSY_MIRROR"] = MIRROR

    k8s_yaml(encode_yaml({
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        # Without the switch there is no ingress rule, so nothing reaches the pod.
        "spec": policy,
    }))

    scratch = {"name": "scratch", "mountPath": SCRATCH}
    hardened = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    k8s_yaml(encode_yaml({
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            # One task per pod, and a surge pod does not fit the pool node, which holds one slot.
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": labels},
            "template": {
                # kubectl logs and exec reach the worker, not the init container, without a -c.
                "metadata": {"labels": labels, "annotations": {"kubectl.kubernetes.io/default-container": name}},
                "spec": {
                    "nodeSelector": {topology["pool_label"]: topology["pools"][pool]},
                    "tolerations": [{
                        "key": topology["pool_label"],
                        "operator": "Equal",
                        "value": topology["pools"][pool],
                        "effect": topology["taint_effect"],
                    }],
                    "automountServiceAccountToken": False,
                    # The service's drain of 30 s, the 5 s group kill of a child and the upload of its log tail.
                    "terminationGracePeriodSeconds": 75,
                    "securityContext": {
                        "runAsUser": USER,
                        "runAsGroup": USER,
                        "runAsNonRoot": True,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    # The fresh volume holds none of the directories the tools write to.
                    "initContainers": [{
                        "name": "scratch",
                        "image": image,
                        "command": ["mkdir", "-p"] + sorted(SCRATCH_ENV.values()),
                        "resources": res,
                        "securityContext": hardened,
                        "volumeMounts": [scratch],
                    }],
                    "containers": [{
                        "name": name,
                        "image": image,
                        "ports": [{"name": "metrics", "containerPort": METRICS_PORT}],
                        "env": [{"name": k, "value": v} for k, v in sorted(env.items())] + [{
                            "name": "DSY_SLOT_MEMORY_MIB",
                            "valueFrom": {"resourceFieldRef": {"resource": "limits.memory", "divisor": "1Mi"}},
                        }],
                        # The service creates the file once it polls, and removes it when it starts to drain.
                        "readinessProbe": {"exec": {"command": ["test", "-e", READY]}, "periodSeconds": 5},
                        "resources": res,
                        "securityContext": hardened,
                        "volumeMounts": [
                            {"name": "store", "mountPath": CREDENTIALS.rsplit("/", 1)[0], "readOnly": True},
                            scratch,
                        ],
                    }],
                    "volumes": [
                        {"name": "store", "secret": {"secretName": name + "-store"}},
                        # On disk, because a memory volume counts against the slot.
                        {"name": "scratch", "emptyDir": {"sizeLimit": "1Gi"}},
                    ],
                },
            },
        },
    }))

    k8s_resource(
        name,
        objects = ["%s-store:secret:%s" % (name, namespace), "%s:networkpolicy:%s" % (name, namespace)],
        labels = [module["group"]],
        resource_deps = [d.split("/")[1] for d in module.get("depends_on", [])],
    )
