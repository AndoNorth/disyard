# Instrumentation contract

Owner epic: #4 (job event envelope and state propagation), until the observability epic (#13) takes the file over.
Version: 2
Consumers: observability (#13) and every component that emits a metric, a log line or a span.

This contract pins the rules for metrics, labels, log fields and traces, and a `metrics.yaml` that holds the metrics of the event contract and the counters of the broker client.
The observability epic extends `metrics.yaml` with the rest of the platform catalogue and owns the contract after the pin.
An epic may add its own metrics to `metrics.yaml` before then, by a contract-change PR.

## Metric names

- A name is `wdf_` followed by lower-case words and digits joined by underscores.
- A counter ends in `_total`, and a gauge or a histogram never does.
- A metric with a unit ends in that unit, in base units (`bytes`, `seconds`, `ratio`), before the `_total` of a counter.
- A histogram declares its bucket bounds in `metrics.yaml`.

## Labels

- A label is never an unbounded identifier.
  The denylist is `x-label-denylist` in [metrics.schema.json](metrics.schema.json), and it holds the job id, task id, event id, trace id and the names that spell them differently.
- Every label declares its bounded set of values in `metrics.yaml`, either as `values` or as `values_from`, the repository directory whose entry names are the values.
  The labels `reason`, `type`, `from_major`, `outcome` and `call` of the metrics below do so.
- The priority class and the job class are the context attributes that are allowed as labels, because each is a configured set.
  They are declared like any other label.
- `le` and `quantile` are reserved.

## Log fields

A structured log line reuses the names of the context attributes: `tenant`, `jobid`, `taskid`, `attempt` and `traceparent`.
It adds `event_type` and `cloudevent_id`, where `cloudevent_id` is the CloudEvents `id`.
The stored event key keeps the name `event_id`, which is the pair `<source>#<id>`, so the two names never mean the same thing.

## Traces

- The trace context is the `traceparent` attribute of the event, as the W3C trace context defines it.
- A stage starts a span per event it handles, parented from the `traceparent` of that event, and an event the stage emits carries the `traceparent` of that span.
- A span carries the context attribute names as attributes, and never puts them in a metric label.
- There is no promise of one trace for a job that lasts days, and a stage that resumes after a pause starts a span linked to the earlier one.

## Metrics of the event contract

| Metric | Type | Labels | Emitted by |
| --- | --- | --- | --- |
| `wdf_event_bytes` | histogram | none | `to_bytes` observes the size of every serialised event |
| `wdf_event_size_warnings_total` | counter | none | `to_bytes` when an event is above the warning size |
| `wdf_event_rejected_total` | counter | `reason` | `to_bytes` when it refuses an event |
| `wdf_event_upgraded_total` | counter | `type`, `from_major` | `read_event` and `upgrade_for_replay` on each upgrade |

The events lane asserts the serializer metrics and the compatibility lane asserts the upgrade metric in their own tests.

## Metrics of the broker contract

Version 2 adds the counters of the broker client, which [contracts/broker/README.md](../broker/README.md) describes.
They live in the in-process registry of `wdf_events.metrics`, and the observability epic exports that registry from every process that loads it.

| Metric | Type | Labels | Emitted by |
| --- | --- | --- | --- |
| `wdf_broker_publish_total` | counter | `outcome`: `accepted`, `rejected`, `unroutable`, `timeout`, `unavailable`, `topology-error` | the client on every publish, as its outcome or error |
| `wdf_broker_settle_total` | counter | `call`: `ack`, `retry`, `dead_letter`, `release` | the client on every settle call of an intake delivery |
| `wdf_broker_dead_letter_total` | counter | `reason`: `MALFORMED`, `UNSUPPORTED_VERSION`, `ADMISSION_FAILED` | the client on every `dead_letter` call, by its application reason |

The `reason` values are the application codes of `contracts/broker/reasons.yaml`, and `packages/broker/tests/test_reasons.py` holds them equal.
Version 2 consumers: admission (#8), observability (#13) and dispatch (#24).

## Loader

`contracts/instrumentation/check.sh <file>` validates a metrics file.
It exits 0 when valid, 1 with `reason: <code>: <detail>` on stderr when not, and 2 on usage.

| Reason | Fails when |
| --- | --- |
| `schema-violation` | the file does not match the schema, which includes an unknown type, a reserved label name and an empty `values` list |
| `bad-metric-name` | a name breaks the naming rule, or a histogram has no buckets |
| `denied-label` | a label is on the denylist |
| `undeclared-label-values` | a label declares neither `values` nor `values_from` |
| `unknown-values-source` | `values_from` is not a directory of the repository |

## Vectors and the loader test

`vectors/valid/*.yaml` must pass, and `vectors/invalid/<reason>/*.yaml` must fail with that reason.
`scripts/verify/cluster/lib/vectors.sh contracts/instrumentation contracts/instrumentation/metrics.yaml` runs them and also loads the pinned file.
`packages/events/tests/test_instrumentation.py` runs the same loader test inside `just ci`.

## Changing it

A change after the pin is a `contract-change` PR that touches `contracts/` only, lists the consumer issues and bumps `Version`.
