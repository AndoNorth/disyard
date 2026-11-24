# Drill: a container dies and restarts

Stop the admission container from the node, so the kubelet restarts it in the same pod.
This is the failure that [DsyPodRestartUnexpected](../alerts.md#dsypodrestartunexpected) exists for, and it needs no scale-down.

Runs on a cluster with the observability stack on, with the [alert watcher](README.md#watch-the-alerts) running.

## Break

The node that holds the pod is the `NODE` column of `kubectl --context kind-dsy -n dsy-control get pods -o wide`, and each kind node is a Docker container of the same name:

```sh
node=$(kubectl --context kind-dsy -n dsy-control get pods -o wide | awk '/^admission/{print $7}')
docker exec "$node" crictl stop "$(docker exec "$node" crictl ps --name admission -q | head -1)"
```

## Observe

| Signal | Where | Seen |
| --- | --- | --- |
| Pods | `kubectl --context kind-dsy -n dsy-control get pods` | `admission ... 1/1 Running 1 (20s ago)`, ready again within 20 s |
| Query | `kube_pod_container_status_restarts_total{container="admission"}` | steps from 0 to 1 |
| Query | `kube_pod_container_status_last_terminated_reason{container="admission"} == 1` | `Error`, and not `OOMKilled`, so the alert reads it as a restart and not as a memory kill |
| Log | `kubectl --context kind-dsy -n dsy-control logs deploy/admission --previous` | the log of the stopped container, ending at its last claimed job |
| Dashboard | `cluster-health`, panel "Container restarts by namespace" | `dsy-control` reads about 1 |
| Alert | [DsyPodRestartUnexpected](../alerts.md#dsypodrestartunexpected) | `firing` 32 s after the stop, as the rule has no `for` window and waits only for the next scrape |

## Recover

Nothing to do: the kubelet restarted the container, and the service was back before the alert fired.
The alert stays up until its 15 minute `increase` window has no restart left (it resolved 903 s after the stop), which is the whole recovery, so a firing alert here says that a restart happened and not that the service is down.
Read `logs --previous` for the cause, as [the entry](../alerts.md#dsypodrestartunexpected) says.
