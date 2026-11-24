"""The models of the broker and tenancy contracts, the source of their generated schemas, and the
publish outcomes and errors of the client API."""

import enum
import re
from importlib import resources
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, StrictBool, model_validator

# The contract files the package reads ship inside it, as links to the repository files.
CONTRACT = resources.files(__package__) / "contract"

# The presets of deploy/presets, each with one tenancy profile.
PRESETS = ("ci", "dev", "soak")
# The x-last-death-reason values RabbitMQ sets.
BROKER_CODES = ("delivery_limit", "rejected", "maxlen", "expired")
APPLICATION_CODE = re.compile("[A-Z][A-Z0-9_]{0,63}")
NAME_MAX_BYTES = 255


class Outcome(enum.Enum):
    """The outcome of a publish, valued as the outcome label of dsy_broker_publish_total."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNROUTABLE = "unroutable"


class TopologyError(Exception):
    """A link refused with amqp:not-found, because the exchange or the queue is missing."""


class PublishTimeout(Exception):
    """No publish outcome within the timeout, under a memory or disk alarm or a stalled broker."""


class BrokerUnavailable(Exception):
    """The connection is lost, and every pending and later call on it raises this."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Estimate(_Model):
    """A figure that a value PR may change, marked as an estimate until it is measured."""

    value: PositiveInt
    estimate: StrictBool


class IntakeExchange(_Model):
    name: str
    type: Literal["direct"]
    durable: Literal[True]


class DeadLetterExchange(_Model):
    name: str
    type: Literal["fanout"]
    durable: Literal[True]


class IntakeQueue(_Model):
    template: str = Field(description="The queue name, with <tenant> and <class> replaced.")
    type: Literal["quorum"]
    initial_group_size: Literal[1]
    binding: Literal["queue-name"] = Field(description="Bound to the intake exchange by its name.")


class Queue(_Model):
    name: str
    type: Literal["quorum"]
    initial_group_size: Literal[1]


class IntakeDefinition(_Model):
    delivery_limit: PositiveInt
    max_length: Estimate
    overflow: Literal["reject-publish"]
    dead_letter_exchange: str
    dead_letter_strategy: Literal["at-least-once"]
    delayed_retry_type: Literal["failed"] = Field(
        description="Only a counted retry is delayed, and an uncounted release returns at once."
    )
    delayed_retry_min: PositiveInt = Field(description="In ms, times the delivery count.")
    delayed_retry_max: PositiveInt = Field(description="In ms, the longest delay.")

    @model_validator(mode="after")
    def _delays(self):
        if self.delayed_retry_min > self.delayed_retry_max:
            raise ValueError("delayed_retry_min is above delayed_retry_max")
        return self


class DlqDefinition(_Model):
    delivery_limit: Literal[-1] = Field(description="-1 turns the delivery limit off.")
    message_ttl: PositiveInt | None = Field(
        default=None, description="Never set: a dead letter leaves only by replay or purge."
    )


class Policy(_Model):
    name: str
    pattern: str
    apply_to: Literal["quorum_queues"]


class IntakePolicy(Policy):
    definition: IntakeDefinition


class DlqPolicy(Policy):
    definition: DlqDefinition


class Addresses(_Model):
    publish: Literal["/exchanges/dsy.intake/<routing key>"]
    consume: Literal["/queues/<queue>"]


class Message(_Model):
    sections: list[Literal["data"]] = Field(min_length=1, max_length=1)
    content_type: Literal["application/cloudevents+json; charset=utf-8"]
    durable: Literal[True]
    application_properties: Literal[False]
    annotations_on_publish: Literal[False]


class Annotations(_Model):
    reason: Literal["x-opt-dsy-reason"]
    detail: Literal["x-opt-dsy-detail"]
    detail_max_chars: Literal[1024]


class Permissions(_Model):
    """RabbitMQ regular expressions on the vhost, where the empty string grants nothing."""

    configure: str
    write: str
    read: str


class Users(_Model):
    dispatch: Permissions
    admission: Permissions
    operator: Permissions


class Ports(_Model):
    amqp: Literal[5672]
    prometheus: Literal[15692]


class Memory(_Model):
    limit_mib: PositiveInt = Field(description="The memory request and limit of the broker.")
    high_watermark_mib: PositiveInt = Field(
        description="Absolute, so it never depends on the memory the container detects."
    )

    @model_validator(mode="after")
    def _watermark(self):
        if self.high_watermark_mib >= self.limit_mib:
            raise ValueError("high_watermark_mib is not below limit_mib")
        return self


class Topology(_Model):
    """topology.yaml: the exchanges, queues, policies, message shape, users, ports and memory."""

    vhost: Literal["/"]
    intake_exchange: IntakeExchange
    intake_queue: IntakeQueue
    intake_policy: IntakePolicy
    dead_letter_exchange: DeadLetterExchange
    dead_letter_queue: Queue
    dlq_policy: DlqPolicy
    addresses: Addresses
    message: Message
    annotations: Annotations
    users: Users
    ports: Ports
    memory: Memory


class Reason(_Model):
    code: str
    source: Literal["application", "broker"]
    read_from: Literal["x-opt-dsy-reason", "x-last-death-reason"]
    on_intake: Literal["expected", "not-expected"]
    event: Literal["job.dead.lettered"] | None
    recovery: Literal["never", "after-fix", "replay"] | None
    action: str = Field(min_length=1)

    @model_validator(mode="after")
    def _source(self):
        if self.source == "application":
            if not APPLICATION_CODE.fullmatch(self.code):
                raise ValueError(f"{self.code}: an application code is upper case")
            if self.read_from != "x-opt-dsy-reason":
                raise ValueError(f"{self.code}: an application code is read from x-opt-dsy-reason")
        else:
            if self.code not in BROKER_CODES:
                raise ValueError(f"{self.code}: a broker code is one of {', '.join(BROKER_CODES)}")
            if self.read_from != "x-last-death-reason":
                raise ValueError(f"{self.code}: a broker code is read from x-last-death-reason")
            if self.event is not None:
                raise ValueError(f"{self.code}: a broker code has no event")
        return self


class Reasons(_Model):
    """reasons.yaml: every dead-letter reason with its event and its recovery."""

    reasons: list[Reason] = Field(min_length=1)


class Caps(_Model):
    open: Estimate = Field(description="Live and paused workflows across tenants.")
    global_: Estimate = Field(alias="global", description="Live workflows across tenants.")
    job_class: dict[str, Estimate] = Field(
        min_length=1, description="Live workflows of each job class of the limits profile."
    )


class Tenant(_Model):
    quota: Estimate = Field(description="Live workflows of the tenant.")


class TenancyProfile(_Model):
    priority_classes: list[str] = Field(
        min_length=1, description="Ordered from first served to last."
    )
    reserve_interval: Estimate = Field(
        description="One admission in this many goes to the lowest waiting class."
    )
    caps: Caps
    tenants: dict[str, Tenant] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self):
        if len(set(self.priority_classes)) != len(self.priority_classes):
            raise ValueError("a priority class is listed twice")
        if self.reserve_interval.value < 2:
            raise ValueError("reserve_interval is at least 2")
        return self


class Tenancy(_Model):
    """tenancy.yaml: one profile per preset, and the tenancy profile of each Tilt profile."""

    tilt_profiles: dict[str, str] = Field(min_length=1)
    profiles: dict[str, TenancyProfile] = Field(min_length=1)


# Each schema: its model and its contract directory.
FILES = {
    "topology": (Topology, "broker"),
    "reasons": (Reasons, "broker"),
    "tenancy": (Tenancy, "tenancy"),
}


def schema_path(name: str) -> str:
    return f"contracts/{FILES[name][1]}/{name}.schema.json"
