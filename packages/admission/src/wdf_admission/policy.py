"""The admission policy: which waiting work one cycle may grant, and in what order, within the caps
of the tenancy profile.

Pure, so a simulator can drive it as the service does. Priority classes are served in strict order,
except that every `reserve_interval`-th grant goes to the lowest class that waits. Inside a class
the granted resumes come first in `updated_at` order, then the tenants take turns, a tenant's turn
granting its oldest re-admission before its intake queue.
"""

from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from wdf_broker.models import TenancyProfile


@dataclass(frozen=True)
class Caps:
    """The figures of a tenancy profile that bound the live and the open jobs."""

    open: int
    global_: int
    quotas: Mapping[str, int]
    job_class: Mapping[str, int]
    reserve_interval: int

    @classmethod
    def of(cls, profile: TenancyProfile, global_cap: int | None = None) -> "Caps":
        """The caps of a profile, with the global cap lowered to `global_cap` when one is given."""
        global_ = profile.caps.global_.value
        return cls(
            open=profile.caps.open.value,
            global_=global_ if global_cap is None else min(global_, global_cap),
            quotas={tenant: t.quota.value for tenant, t in profile.tenants.items()},
            job_class={name: cap.value for name, cap in profile.caps.job_class.items()},
            reserve_interval=profile.reserve_interval.value,
        )


@dataclass(frozen=True)
class Snapshot:
    """The counts of one cycle, read from the job rows."""

    # Rows in `admitted` or `running`, by tenant and job class.
    live: Mapping[tuple[str, str], int]
    # The live rows plus the rows in `paused` and `resume-pending`.
    open: int
    # Resumes granted whose rows are still `resume-pending`, by tenant and job class.
    granted: Mapping[tuple[str, str], int] = field(default_factory=dict)


@dataclass(frozen=True)
class _Job:
    job_id: str
    priority_class: str
    tenant: str
    job_class: str
    updated_at: datetime


@dataclass(frozen=True)
class Resume(_Job):
    """A `resume-pending` row whose resume is requested and not yet granted."""


@dataclass(frozen=True)
class Readmission(_Job):
    """A row resumed before it started, whose intake message is already consumed."""


@dataclass(frozen=True)
class Queue:
    """An intake queue that may hold work. Its job class is known only after a pull, unless the
    queue is blocked on the class of the message it released last."""

    name: str
    tenant: str
    priority_class: str
    blocked_on: str | None = None


Grant = Resume | Readmission | Queue


@dataclass(frozen=True)
class Turn:
    """What one cycle hands the next: the grants made so far, which the reserve counts, and the
    tenant each priority class served last, so tenants keep their turns when few slots free."""

    grants: int = 0
    last: Mapping[str, str] = field(default_factory=dict)

    def after(self, grants: Iterable[Grant]) -> "Turn":
        """The turn once `grants` are carried out, which a cycle passes to its next plan."""
        count, last = self.grants, dict(self.last)
        for grant in grants:
            count += 1
            if not isinstance(grant, Resume):
                last[grant.priority_class] = grant.tenant
        return Turn(count, last)


class _Counts:
    def __init__(self, snapshot: Snapshot, caps: Caps):
        self.caps = caps
        rows = Counter(snapshot.live) + Counter(snapshot.granted)
        self.live = sum(rows.values())
        self.open = snapshot.open
        self.tenant = Counter()
        self.job_class = Counter()
        for (tenant, job_class), n in rows.items():
            self.tenant[tenant] += n
            self.job_class[job_class] += n

    def fits(self, tenant: str, job_class: str | None, opens: bool) -> bool:
        caps = self.caps
        return (
            self.live < caps.global_
            and self.tenant[tenant] < caps.quotas.get(tenant, 0)
            and (job_class is None or self.job_class[job_class] < caps.job_class.get(job_class, 0))
            and (not opens or self.open < caps.open)
        )

    def take(self, tenant: str, job_class: str | None, opens: bool) -> None:
        self.live += 1
        self.tenant[tenant] += 1
        if job_class is not None:
            self.job_class[job_class] += 1
        if opens:
            self.open += 1


def plan(
    snapshot: Snapshot,
    caps: Caps,
    priority_classes: Sequence[str],
    waiting: Iterable[Grant],
    *,
    turn: Turn | None = None,
) -> list[Grant]:
    """The grants of one cycle, in the order to carry them out.

    A queue may be granted many times, once per message to pull. The job class of a pulled message
    is not counted here, except for the first pull of a blocked queue, so intake checks it against
    its cap with the earlier grants of the cycle counted.
    """
    turn = turn or Turn()
    counts = _Counts(snapshot, caps)
    resumes: dict[str, list[Resume]] = {}
    readmissions: dict[tuple[str, str], list[Readmission]] = {}
    queues: dict[tuple[str, str], Queue] = {}
    for item in waiting:
        if isinstance(item, Resume):
            resumes.setdefault(item.priority_class, []).append(item)
        elif isinstance(item, Readmission):
            readmissions.setdefault((item.priority_class, item.tenant), []).append(item)
        else:
            queues[(item.priority_class, item.tenant)] = item
    for jobs in (*resumes.values(), *readmissions.values()):
        jobs.sort(key=lambda job: job.updated_at)
    tenants = {
        cls: sorted({tenant for c, tenant in (*readmissions, *queues) if c == cls})
        for cls in priority_classes
    }

    def next_of(cls: str) -> Grant | None:
        for resume in resumes.get(cls, ()):
            if counts.fits(resume.tenant, resume.job_class, opens=False):
                return resume
        order = tenants[cls]
        start = bisect_right(order, turn.last[cls]) if cls in turn.last else 0
        for tenant in order[start:] + order[:start]:
            for job in readmissions.get((cls, tenant), ()):
                if counts.fits(tenant, job.job_class, opens=True):
                    return job
            queue = queues.get((cls, tenant))
            if queue and counts.fits(tenant, queue.blocked_on, opens=True):
                return queue
        return None

    grants: list[Grant] = []
    while True:
        offers = [(cls, grant) for cls in priority_classes if (grant := next_of(cls))]
        if not offers:
            return grants
        reserve = (turn.grants + 1) % caps.reserve_interval == 0
        cls, grant = offers[-1] if reserve else offers[0]
        if isinstance(grant, Resume):
            counts.take(grant.tenant, grant.job_class, opens=False)
            resumes[cls].remove(grant)
        elif isinstance(grant, Readmission):
            counts.take(grant.tenant, grant.job_class, opens=True)
            readmissions[(cls, grant.tenant)].remove(grant)
        else:
            # A blocked queue's first pull returns the message it released, of its blocked class.
            counts.take(grant.tenant, grant.blocked_on, opens=True)
            queues[(cls, grant.tenant)] = replace(grant, blocked_on=None)
        grants.append(grant)
        turn = turn.after([grant])
