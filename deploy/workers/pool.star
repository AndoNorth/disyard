# The module of one pool worker, loaded by deploy/workers/<pool>/Tiltfile.module: the pool's image, one Deployment
# <pool>-worker of one single-task pod on the pool's node, the Secret of the pool's own store identity, and the
# NetworkPolicy that limits the pod's egress to the platform endpoints the pool needs.
# The queue is unversioned and no build id reaches the pod, so a pod serves the workflows of every build.
# Relative paths resolve against the calling module's directory.

# The scratch volume and what lives on it: task scratch, the wasmtime module cache, HOME, the caches and the ready file.
SCRATCH = "/var/wdf"
SCRATCH_ENV = {
    "HOME": SCRATCH + "/home",
    "TMPDIR": SCRATCH + "/tmp",
    "XDG_CACHE_HOME": SCRATCH + "/cache",
    "UV_CACHE_DIR": SCRATCH + "/cache/uv",
}
READY = SCRATCH + "/ready"
CREDENTIALS = "/etc/wdf/store/credentials.json"
# The user every pool image runs as.
USER = 65532
MIRROR = "http://mirror.wdf-infra.svc.cluster.local"

# The pods each pool may reach, as namespace, pod labels and target port. Every pool needs DNS, the Temporal frontend
# and the object store, and the compile pool also the package mirror for its dependency fetch.
DNS = ("kube-system", {"k8s-app": "kube-dns"}, 53)
TEMPORAL = ("wdf-infra", {"app.kubernetes.io/name": "temporal", "app.kubernetes.io/component": "frontend"}, 7233)
STORE = ("wdf-infra", {"app": "objectstore"}, 8333)
MIRROR_POD = ("wdf-infra", {"app": "mirror"}, 8080)

def _svc(service, port):
    return "%s.wdf-infra.svc.cluster.local:%d" % (service, port)

def _egress(peer, protocols = ["TCP"]):
    namespace, labels, port = peer
    return {
        "to": [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
            "podSelector": {"matchLabels": labels},
        }],
        "ports": [{"protocol": p, "port": port} for p in protocols],
    }

def pool_worker(pool):
    module = read_yaml("module.yaml")
    name = module["name"]
    namespace = module["namespace"]
    res = module["resources"][name]
    topology = read_yaml("../../../contracts/topology/topology.yaml")
    naming = read_yaml("../../../contracts/temporal/naming.yaml")
    labels = {"app.kubernetes.io/name": name}

    image = "wdf-" + name
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
        "WDF_TEMPORAL_ADDRESS": _svc("temporal-frontend", TEMPORAL[2]),
        "WDF_NAMESPACE": naming["namespace"],
        "WDF_STORE_ENDPOINT": "http://" + _svc("objectstore", STORE[2]),
        "WDF_STORE_CREDENTIALS": CREDENTIALS,
        "WDF_SCRATCH": SCRATCH,
        "WDF_READY_FILE": READY,
    }
    env.update(SCRATCH_ENV)
    if pool == "compile":
        egress.append(_egress(MIRROR_POD))
        env["WDF_MIRROR"] = MIRROR

    k8s_yaml(encode_yaml({
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "podSelector": {"matchLabels": labels},
            # No ingress rule, so nothing reaches the pod.
            "policyTypes": ["Ingress", "Egress"],
            "egress": egress,
        },
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
                "metadata": {"labels": labels},
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
                        "env": [{"name": k, "value": v} for k, v in sorted(env.items())] + [{
                            "name": "WDF_SLOT_MEMORY_MIB",
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
