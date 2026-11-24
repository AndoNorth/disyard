# dsy-clusterkit

The host side of every driver of a deployed cluster, imported as `dsy_clusterkit`.
The workflow harness's T2 tier, the admission drive and the dispatch smoke driver build on it.

It owns `kubectl` on a kube context, `Forward`, a `kubectl port-forward` bound to 127.0.0.1 on a fixed local port that survives a pod restart, the state directory and the credentials in it, `reachable`, `ready` and `oom_killed` for pods, the `until` poller, and the namespace and start-up constants.
What a driver does with the cluster, its journeys, its counters and the deployments it scales or restarts, stays in the driver.

It is test tooling, so no service depends on it.
The packages whose drivers use it name it in their `cluster` extra, which no image installs, and the dev group of the workspace installs it.
It imports none of the services.
