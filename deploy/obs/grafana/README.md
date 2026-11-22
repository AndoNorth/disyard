# Grafana

Dashboards, Explore and annotations of the observability stack: Grafana from the pinned image of `deploy/versions.yaml`, in `wft-obs` on the obs node.
The module is group `obs`, profile `ci`, and runs only on a cluster brought up with `WFT_OBS=on`, as the [stack overview](../README.md) says.

## Files

| File | Holds |
| --- | --- |
| `module.yaml` | the module manifest: Grafana at 100m of CPU and 512 MiB, requests equal to limits |
| `Tiltfile.module` | loads the Secret of `secrets.sh` and the objects that `config.py` renders |
| `config.py` | renders the datasources, the dashboard provider and every object of the module, and prints the datasource file with `datasources` |
| `secrets.sh` | generates the admin credential into the state directory and prints it as a Secret or as a bare value |
| `dashboards/` | the provisioned dashboards, listed in [their own README](dashboards/README.md) |

## Datasources

| uid | Backend | Notes |
| --- | --- | --- |
| `prometheus` | `prometheus.wft-obs:9090` | the default datasource |
| `loki` | `loki.wft-obs:3100` | derived fields, below |
| `tempo` | `tempo.wft-obs:3200` | |
| `pyroscope` | `pyroscope.wft-obs:4040` | |

Tempo and Pyroscope are provisioned before their modules exist, and Grafana does not check a datasource at provisioning, so a query on either answers an error until its backend runs.

The Loki datasource has two derived fields, each read from the structured metadata of a log line by label name:

| Field | Opens |
| --- | --- |
| `trace_id` | the trace of that id in the Tempo datasource |
| `job_id` | the job's workflow page in the Temporal UI, `http://127.0.0.1:18233/namespaces/wft/workflows/job-<job id>` |

The Tempo datasource has no `jsonData` setting for a link from a span attribute to an external URL, so the link is a provisioned correlation of type `external` on the `temporalWorkflowID` field.
A correlation binds to a field of a result table and not to a span tag, so the link shows where a TraceQL query returns the attribute as a column, such as `{ resource.service.name = "admission" } | select(span.temporalWorkflowID)` with the table format set to Spans: each value opens the job's workflow page in the Temporal UI.
It does not show among a span's attributes in the trace view, which renders no link from a correlation, and a span without the attribute gets a link to the workflow list.

## Access

| What | How |
| --- | --- |
| Browse and Explore | `http://127.0.0.1:13000`, with no login: an anonymous visitor is an Editor, the role that gives Explore, and the port is bound to loopback |
| The HTTP API | basic auth as user `admin` with the value of `deploy/obs/grafana/secrets.sh password`, on `http://127.0.0.1:13000/api/...` |

The admin credential is generated on first use into `grafana.credential` in the state directory (`WFT_STATE_DIR`, else `$XDG_STATE_HOME/weft`), and the Secret carries it into the pod.
It is never committed.
Grafana keeps the password in its database from its first start, so deleting the file while the cluster lives breaks the match, and `just nuke` resets both.
Grafana reports the anonymous role setting as deprecated in favour of Viewer only, and the Editor role still gives Explore on the pinned version.

## Annotations and persistence

Grafana's database is on a 1 GiB local-path volume, so an annotation posted to `POST /api/annotations` survives a pod replacement and ends with `just nuke`.
The strategy is `Recreate`: the volume takes one writer, and a second Grafana beside the first would not fit the node.
The preinstalled drilldown apps are switched off, because they download about 250 MB on every first start and no dashboard reads them.

## Dashboards

Each file of `dashboards/` is provisioned as it says and cannot be edited in the UI.
The dashboards are mounted from a ConfigMap, so a change to a file reaches the running pod without a restart.
A ConfigMap holds 1 MiB, and the dashboards share one.
