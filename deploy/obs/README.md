# Observability stack

Metrics, logs, traces and profiles of the platform, viewed in Grafana.
The stack runs only on a cluster brought up with the observability switch on, and GitHub CI never deploys it.
How the switch sizes the cluster and which host ports it opens is in [docs/dev-setup.md](../../docs/dev-setup.md) and [docs/cluster-host.md](../../docs/cluster-host.md).
The rules every component follows are the [instrumentation contract](../../contracts/instrumentation/README.md), and the shape is [diagram 05](../../docs/architecture/05-observability-data-flow.drawio.svg).

## Turning it on

```sh
WFT_OBS=on just up
```

The switch is chosen when the cluster is created, so a cluster brought up without it stays without the stack until `just nuke`.
`just smoke` never turns the stack on, and it checks the stack only on a cluster that has it.

## What runs

Every module is group `obs`, in namespace `wft-obs`, on the obs node.

| Component | Role |
| --- | --- |
| Prometheus | Scrapes every pod's `metrics` port, RabbitMQ's detailed families, and each node's kubelet and cAdvisor; holds the alert rules; receives the smoke gate's results by OTLP |
| kube-state-metrics | Pod, deployment and node state |
| Collector | One OpenTelemetry Collector that receives logs and traces from the Python services by OTLP and watches Kubernetes events |
| Loki | Logs and Kubernetes events, correlation ids in structured metadata |
| Tempo | Traces |
| Pyroscope | Profiles pushed by the Python services |
| Grafana | Dashboards, alerts and Explore, at `127.0.0.1:13000` |
| Temporal UI | The per-job view, at `127.0.0.1:18233`, on the infra node |

## Following one job

1. In Grafana, open Explore on the Loki datasource and query by job id, such as `{service_name=~".+"} | job_id="<job id>"`.
   Each line's `trace_id` links to its trace in Tempo, and its `job_id` links to the job's workflow page in the Temporal UI.
2. The Temporal UI shows the job's timeline at `http://127.0.0.1:18233/namespaces/wft/workflows/job-<job id>`.
3. Without the UI, the timeline comes from the CLI through a forward of the Temporal frontend:

   ```sh
   kubectl --context kind-wft -n wft-infra port-forward --address 127.0.0.1 svc/temporal-frontend 17233:7233
   temporal workflow show --address 127.0.0.1:17233 --namespace wft --workflow-id job-<job id>
   ```

Temporal keeps a closed workflow's history for 24 hours, so the Temporal UI and the CLI lose a finished job's timeline a day after it ends.
Its logs, traces and metrics stay in the stack for their own retention.
The 24 hours are revisited with the soak work, https://github.com/AndoNorth/weft/issues/16.

## Reading the stack from code

`wft_obs.query` in [packages/obs](../../packages/obs/README.md) is the one client of the Prometheus, Loki, Tempo and Pyroscope APIs, and `wft_obs.testing.FakeObs` stands in for them in tests.
