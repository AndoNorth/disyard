# Drill: a malformed message reaches intake

Publish a truncated event to the intake queue, as a producer with a defect would.
Admission cannot read it and moves it to the dead-letter queue with the reason `MALFORMED`.

Runs on a cluster with the observability stack on, with the [alert watcher](README.md#watch-the-alerts) running.

## Break

Forward the broker, then publish the `truncated-json` poison vector with the dispatch login:

```sh
kubectl --context kind-wft -n wft-infra port-forward --address 127.0.0.1 pod/rabbitmq-0 5672:5672 15692:15692
```

```sh
uv run --frozen python - <<'EOF'
from pathlib import Path
from wft_broker.client import Credentials, connect
from wft_compat.vectors import POISON

password = (Path.home() / ".local/state/weft/rabbitmq/dispatch.password").read_text().strip()
connection = connect("127.0.0.1:5672", Credentials("dispatch", password))
print(connection.publish("intake.acme.standard", (POISON / "truncated-json.json").read_bytes()))
connection.close()
EOF
```

It prints `Outcome.ACCEPTED`, so the broker took the message and the failure is downstream of it.

## Observe

| Signal | Where | Seen |
| --- | --- | --- |
| Tool | `uv run --frozen python -m wft_broker.dlq list --address 127.0.0.1:5672 --credentials-dir ~/.local/state/weft/rabbitmq` | `1 MALFORMED malformed-json - - - intake.acme.standard <time>`, then `count MALFORMED 1` |
| Tool | the same with `show` | the reason `MALFORMED`, the detail `malformed-json: Unterminated string starting at: line 1 column 97 (char 96)` and the first death queue `intake.acme.standard` |
| Query | `wft_admission_dead_lettered_total` | `MALFORMED` is 1, the other two reasons 0 |
| Dashboard | `intake-admission` and `fleet`, panels "Dead letters by reason /s" | a point for the reason |
| Alert | [WftAdmissionDeadLettered](../alerts.md#wftadmissiondeadlettered) and [WftDeadLetterQueueNotEmpty](../alerts.md#wftdeadletterqueuenotempty) | see below |
| Log | `kubectl --context kind-wft -n wft-control logs deploy/admission` | no line about the message, see the finding below |

Real run, seconds counted from the publish:

| At | What |
| --- | --- |
| +35 s | `WftDeadLetterQueueNotEmpty` `pending` |
| +45 s | `WftAdmissionDeadLettered{reason="MALFORMED"}` `firing`, since it has no `for` window and waits only for the next scrape |
| +336 s | `WftDeadLetterQueueNotEmpty` `firing` after its 5 minute window |

The `admission` log has no line for the dead-lettered message, neither in `kubectl logs` nor in Loki, so the entry's advice to read it for the job finds nothing.
A malformed message has no job id to look up in any case, and `dlq show` carries the whole reason and the body.

## Recover

The reason never recovers, so purge it:

```sh
uv run --frozen python -m wft_broker.dlq show --address 127.0.0.1:5672 --credentials-dir ~/.local/state/weft/rabbitmq
uv run --frozen python -m wft_broker.dlq purge --reason MALFORMED --yes --address 127.0.0.1:5672 --credentials-dir ~/.local/state/weft/rabbitmq
```

Real run: `list` prints nothing once the queue is empty, and `WftDeadLetterQueueNotEmpty` resolved 18 s after the purge.
`WftAdmissionDeadLettered` stays up until its 10 minute `increase` window passes, and resolved about 10 minutes after the publish, with nothing to do for it.
There was no job to cancel, because a malformed event names none.

## What the runbooks say

[WftAdmissionDeadLettered](../alerts.md#wftadmissiondeadlettered) lists `dlq show` and `dlq purge --reason MALFORMED --yes` as the way out, and both worked as written.
