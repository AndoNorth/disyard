# dsy-obs

The observability helper of every Python service, and the one read client of the observability stack, imported as `dsy_obs`.
The rules it follows are the [instrumentation contract](../../contracts/instrumentation/README.md), version 5, and the stack it reads is described in [deploy/obs](../../deploy/obs/README.md).

## Dependencies

`pyproject.toml` declares the dependency set of every module the package holds or will hold: `temporalio`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, `pyroscope-io`, `pyyaml`, and `dsy-events`, with `dsy-stateops` in the optional extra `stateops`, which the process that runs the state inputs exporter installs.
`tests/test_wheel.py` builds the wheel, installs it with only those dependencies into a clean virtual environment, imports every module and `pyroscope`, and runs the client against the fake, so a module that imports an undeclared dependency fails there.

## State

Delivered:

- `dsy_obs.query`, the client every harness and the smoke gate read the stack through.
  Each call takes the base address of its backend and raises `QueryError` when the backend does not answer, answers an error status, or does not know a trace.
  Times are Unix seconds.
  - `prom(address, expr)`: the instant vector of a PromQL expression, from `/api/v1/query`.
  - `targets(address)`: Prometheus's active targets, each with `labels` and `health`.
  - `loki(address, query, start, end)`: the streams of a LogQL query, from `/loki/api/v1/query_range`.
  - `trace(address, trace_id)`: the trace of a hex id from Tempo's `/api/traces/<trace id>`, as OTLP JSON.
  - `profiled(address, start, end)`: the `service_name` values of Pyroscope's profiles in the window.
- `dsy_obs.targets.JOBS`, the set of scrape `job` labels of the stack's Prometheus.
  The rendered Prometheus configuration is held equal to it, and the telemetry row of the smoke gate compares the live `up` series to it.
- `dsy_obs.testing.FakeObs`, one `ThreadingHTTPServer` on 127.0.0.1 that answers every call of `dsy_obs.query` from canned data (`vectors`, `active_targets`, `streams`, `traces`, `services`).
  It records the bodies posted to Prometheus's OTLP receiver, `/api/v1/otlp/v1/metrics`, in `pushed`, and Grafana's `POST /api/annotations` in `annotations`.
  Use it as a context manager and pass its `url` as the address of any backend.

Not written yet, with the shape the services build on:

- `dsy_obs.setup(component)`, which installs logs, traces and profiles once per process.
- `dsy_obs.logs`, with `bind(**fields)` taking the log fields of the contract and a `traceparent` argument.
- `dsy_obs.metrics`, with `runtime(address, buckets)` and `export_registry(meter)`.
- `dsy_obs.traces` and `dsy_obs.profiles`.
- `python -m dsy_obs.inputs`, the state inputs exporter.

## Tests

- `tests/test_query.py`: each call sends what its backend expects and reads the fake's answer.
- `tests/test_conformance.py`: the same calls over `FakeObs`, and with `-m cluster` over the real backends, whose base addresses come from `DSY_OBS_PROMETHEUS_URL`, `DSY_OBS_LOKI_URL`, `DSY_OBS_TEMPO_URL` and `DSY_OBS_PYROSCOPE_URL`.
  A backend whose variable is unset is skipped, and the unit run never selects the `cluster` cases.
- `tests/test_catalogue_names.py`: every name an emitter declares in the `COUNTERS`, `GAUGES`, `AGES` and `HISTOGRAMS` of a `metrics.py`, and every name the job workflow creates, is in `contracts/instrumentation/metrics.yaml`, whose `exit_class` values equal the classes of `contracts/workload/exit-classes.yaml`.
- `tests/test_log_names.py`: no `extra=` key and no `logs.bind` keyword under `packages/*/src` is a version 3 log field (`jobid`, `taskid`, `attempt`, `traceparent`), except the `traceparent` argument of `logs.bind`.
