"""The admission stage: the events that admission writes about the job a message names."""

from typing import Any

from dsy_events import Event, build_event
from dsy_keys import workflow_id
from dsy_stages import Facts
from dsy_stages.facts import SAMPLE_DATA

SOURCE = "/dsy/admission"


def admission(inbound: Any, facts: Facts) -> list[Event]:
    """One event per type of `facts.emit`, `job.admitted`, `job.published` or `job.dead.lettered`,
    with the identity of the accepted event `inbound` and the time of `facts`.

    `job.admitted` names the job's workflow, `job.published` carries no data, and the data of
    `job.dead.lettered` is the reason and detail in `facts.data`, which overrides the contract's
    sample as `Facts` defines.
    """
    who = {
        "tenant": inbound.tenant,
        "jobid": inbound.jobid,
        "priorityclass": inbound.priorityclass,
        "jobclass": inbound.jobclass,
        "traceparent": inbound.traceparent,
    }
    events = []
    for name in facts.emit:
        if name == "job.admitted":
            data = {"workflow_id": workflow_id(who["jobid"])}
        else:
            data = facts.data.get(name, SAMPLE_DATA[name])
        events.append(build_event(name, source=SOURCE, time=facts.time, data=data, **who))
    return events
