"""The admission policy over snapshots and waiting work, with no runtime: the caps it never passes,
the order of its grants, and the fairness between tenants and priority classes."""

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from dsy_admission.policy import Caps, Queue, Readmission, Resume, Snapshot, Turn, plan
from dsy_broker.tenancy import load
from hypothesis import given
from hypothesis import strategies as st

CLASSES = ["gold", "silver", "bronze"]
TENANTS = ["a", "b", "c"]
JOB_CLASSES = ["short", "long"]
EPOCH = datetime(2026, 11, 17, tzinfo=UTC)


def queue(tenant: str, cls: str = "standard", blocked_on: str | None = None) -> Queue:
    return Queue(f"intake.{tenant}.{cls}", tenant, cls, blocked_on)


def job(kind, n: int, tenant: str, cls: str = "standard", job_class: str = "short"):
    at = EPOCH + timedelta(seconds=n)
    return kind(f"{kind.__name__}-{tenant}-{n}", cls, tenant, job_class, at)


def wide(**figures) -> Caps:
    """Caps that bind only where a test lowers a figure."""
    base = dict(
        open=10_000,
        global_=10_000,
        quotas={tenant: 10_000 for tenant in TENANTS},
        job_class={job_class: 10_000 for job_class in JOB_CLASSES},
        reserve_interval=5,
    )
    return Caps(**base | figures)


@st.composite
def scenarios(draw):
    """Random caps, counts, waiting work and turn, small enough that the caps bind and loose
    enough that most plans grant something."""
    counts = st.integers(0, 1)
    figure = st.integers(1, 10)
    caps = Caps(
        open=draw(st.integers(1, 30)),
        global_=draw(st.integers(1, 20)),
        quotas={tenant: draw(figure) for tenant in TENANTS},
        job_class={job_class: draw(figure) for job_class in JOB_CLASSES},
        reserve_interval=draw(st.integers(2, 4)),
    )
    pairs = [(tenant, job_class) for tenant in TENANTS for job_class in JOB_CLASSES]
    live = {pair: draw(counts) for pair in pairs}
    granted = {pair: draw(counts) for pair in pairs if draw(st.booleans())}
    paused = draw(st.integers(0, 3))
    snapshot = Snapshot(live, sum(live.values()) + sum(granted.values()) + paused, granted)
    classes = CLASSES[: draw(st.integers(1, 3))]
    fields = st.tuples(
        st.sampled_from(classes), st.sampled_from(TENANTS), st.sampled_from(JOB_CLASSES)
    )
    waiting = []
    for kind in (Resume, Readmission):
        for n, (cls, tenant, job_class) in enumerate(draw(st.lists(fields, max_size=8))):
            waiting.append(job(kind, draw(st.integers(0, 50)) * 10 + n, tenant, cls, job_class))
    for cls in classes:
        for tenant in TENANTS:
            if draw(st.booleans()):
                waiting.append(queue(tenant, cls, draw(st.sampled_from([None, *JOB_CLASSES]))))
    last = {cls: draw(st.sampled_from(TENANTS)) for cls in classes if draw(st.booleans())}
    turn = Turn(draw(st.integers(0, 10)), last)
    return snapshot, caps, classes, waiting, turn


class Oracle:
    """The counts a plan moves, kept apart from the policy so the caps are checked independently."""

    def __init__(self, snapshot: Snapshot, caps: Caps):
        self.caps = caps
        rows = Counter(snapshot.live) + Counter(snapshot.granted)
        self.live, self.open = sum(rows.values()), snapshot.open
        self.tenant = Counter({t: sum(n for (r, _), n in rows.items() if r == t) for t in TENANTS})
        self.job_class = Counter(
            {c: sum(n for (_, r), n in rows.items() if r == c) for c in JOB_CLASSES}
        )

    @staticmethod
    def needs(grant) -> tuple[str | None, bool]:
        """The job class a grant counts against, and whether it opens a workflow."""
        if isinstance(grant, Queue):
            return grant.blocked_on, True
        return grant.job_class, isinstance(grant, Readmission)

    def fits(self, grant) -> bool:
        job_class, opens = self.needs(grant)
        caps = self.caps
        return (
            self.live < caps.global_
            and self.tenant[grant.tenant] < caps.quotas[grant.tenant]
            and (job_class is None or self.job_class[job_class] < caps.job_class[job_class])
            and (not opens or self.open < caps.open)
        )

    def take(self, grant) -> None:
        assert self.fits(grant), grant
        job_class, opens = self.needs(grant)
        self.live += 1
        self.tenant[grant.tenant] += 1
        if job_class:
            self.job_class[job_class] += 1
        self.open += opens


def between(after: str | None, before: str, tenant: str) -> bool:
    """Whether `tenant` comes strictly between two turns in the cyclic order of tenant names."""
    if after is None:
        return tenant < before
    if after < before:
        return after < tenant < before
    return tenant > after or tenant < before


@given(scenarios())
def test_policy_never_passes_a_cap_and_stops_only_when_nothing_fits(scenario):
    snapshot, caps, classes, waiting, turn = scenario
    grants = plan(snapshot, caps, classes, waiting, turn=turn)
    oracle = Oracle(snapshot, caps)
    for grant in grants:
        oracle.take(grant)
    granted = {g.job_id for g in grants if not isinstance(g, Queue)}
    pulled = {g.name for g in grants if isinstance(g, Queue)}
    for item in waiting:
        if isinstance(item, Queue):
            # A pulled queue is no longer blocked.
            left = replace(item, blocked_on=None) if item.name in pulled else item
            assert not oracle.fits(left), item
        elif item.job_id not in granted:
            assert not oracle.fits(item), item


@given(scenarios())
def test_policy_grants_only_resumes_at_the_open_cap(scenario):
    snapshot, caps, classes, waiting, turn = scenario
    full = Snapshot(snapshot.live, max(snapshot.open, caps.open), snapshot.granted)
    grants = plan(full, caps, classes, waiting, turn=turn)
    assert all(isinstance(g, Resume) for g in grants)
    oracle = Oracle(snapshot, caps)
    if any(isinstance(item, Resume) and oracle.fits(item) for item in waiting):
        assert grants


@given(scenarios())
def test_policy_grants_resumes_first_and_a_readmission_before_its_tenants_queue(scenario):
    snapshot, caps, classes, waiting, turn = scenario
    grants = plan(snapshot, caps, classes, waiting, turn=turn)
    for cls in classes:
        mine = [g for g in grants if g.priority_class == cls]
        kinds = [type(g) for g in mine]
        assert kinds == sorted(kinds, key=lambda kind: kind is not Resume)
        resumed = [g.updated_at for g in mine if isinstance(g, Resume)]
        assert resumed == sorted(resumed)
        for tenant in TENANTS:
            theirs = [g for g in mine if g.tenant == tenant and not isinstance(g, Resume)]
            kinds = [type(g) for g in theirs]
            assert kinds == sorted(kinds, key=lambda kind: kind is Queue)
            readmitted = [g.updated_at for g in theirs if isinstance(g, Readmission)]
            assert readmitted == sorted(readmitted)


@given(scenarios())
def test_policy_serves_the_tenants_of_a_class_round_robin(scenario):
    snapshot, caps, classes, waiting, turn = scenario
    grants = plan(snapshot, caps, classes, waiting, turn=turn)
    for cls in classes:
        turns = [g.tenant for g in grants if g.priority_class == cls and not isinstance(g, Resume)]
        previous = turn.last.get(cls)
        for i, tenant in enumerate(turns):
            skipped = {t for t in TENANTS if between(previous, tenant, t)}
            assert not skipped & set(turns[i:]), (previous, turns)
            previous = tenant


@given(scenarios())
def test_policy_grants_only_waiting_work_and_unblocks_a_queue_after_one_pull(scenario):
    snapshot, caps, classes, waiting, turn = scenario
    grants = plan(snapshot, caps, classes, waiting, turn=turn)
    jobs = {item.job_id for item in waiting if not isinstance(item, Queue)}
    queues = {item.name: item for item in waiting if isinstance(item, Queue)}
    for grant in grants:
        assert grant.priority_class in classes
        if isinstance(grant, Queue):
            assert grant.name in queues
        else:
            assert grant.job_id in jobs
    for name, item in queues.items():
        pulls = [g.blocked_on for g in grants if isinstance(g, Queue) and g.name == name]
        assert pulls[1:] == [None] * (len(pulls) - 1)
        assert pulls[:1] in ([], [item.blocked_on])


@given(st.integers(2, 6), st.integers(0, 20), st.integers(1, 3), st.integers(1, 40))
def test_policy_gives_every_reserve_interval_th_grant_to_the_lowest_waiting_class(
    interval, done, tenants, total
):
    caps = wide(global_=total, reserve_interval=interval)
    waiting = [queue(t, cls) for cls in CLASSES for t in TENANTS[:tenants]]
    grants = plan(Snapshot({}, 0), caps, CLASSES, waiting, turn=Turn(done))
    assert len(grants) == total
    for i, grant in enumerate(grants, start=done + 1):
        assert grant.priority_class == ("bronze" if i % interval == 0 else "gold")


def test_policy_reserve_skips_a_class_with_nothing_that_fits():
    caps = wide(reserve_interval=2, global_=4, quotas={"a": 10, "b": 10, "c": 0})
    waiting = [queue("a", "gold"), queue("b", "silver"), queue("c", "bronze")]
    grants = plan(Snapshot({}, 0), caps, CLASSES, waiting)
    assert [g.priority_class for g in grants] == ["gold", "silver", "gold", "silver"]


def test_policy_blocked_queue_waits_for_its_class_only():
    caps = wide(job_class={"short": 3, "long": 5})
    full = Snapshot({("a", "short"): 3}, 3)
    waiting = [
        queue("a", blocked_on="short"),
        queue("b", blocked_on="long"),
        job(Resume, 1, "c"),
        job(Resume, 2, "c", job_class="long"),
        job(Readmission, 3, "c"),
        job(Readmission, 4, "c", job_class="long"),
        queue("c"),
    ]
    grants = plan(full, caps, ["standard"], waiting)
    assert all(not isinstance(g, Queue) or g.tenant != "a" for g in grants)
    assert all(isinstance(g, Queue) or g.job_class == "long" for g in grants)
    assert grants[:3] == [waiting[3], queue("b", blocked_on="long"), waiting[5]]
    assert len(grants) == caps.global_ - 3
    free = plan(Snapshot({("a", "short"): 2}, 2), caps, ["standard"], waiting[:1])
    assert free[0] == waiting[0] and free[1:] == [queue("a")] * (len(free) - 1)


def test_policy_queue_left_out_of_waiting_gets_no_grant():
    grants = plan(Snapshot({}, 0), wide(global_=6), ["standard"], [queue("a"), queue("c")])
    assert {g.tenant for g in grants} == {"a", "c"}


def test_flooding_tenant_cannot_starve_quiet_one():
    caps = wide(global_=1000, quotas={"flood": 1000, "quiet": 1000})
    depth = {"flood": 10_000, "quiet": 10}
    grants = plan(Snapshot({}, 0), caps, ["standard"], [queue(t) for t in depth])
    assert {g.tenant for g in grants[:20]} == {"flood", "quiet"}
    # At the global cap one slot frees per cycle, and the turn carries from cycle to cycle.
    at_cap = Snapshot({("flood", "short"): 999}, 999)
    turn, served = Turn(), []
    for _ in range(20):
        waiting = [queue(tenant) for tenant, left in depth.items() if left]
        [grant] = plan(at_cap, caps, ["standard"], waiting, turn=turn)
        turn = turn.after([grant])
        depth[grant.tenant] -= 1
        served.append(grant.tenant)
    assert served == ["flood", "quiet"] * 10


def test_readmissions_cannot_jump_tenants():
    waiting = [job(Readmission, n, "a") for n in range(1000)] + [queue("b")]
    grants = plan(Snapshot({}, 0), wide(), ["standard"], waiting)
    assert [type(g).__name__ + g.tenant for g in grants[:4]] == [
        "Readmissiona",
        "Queueb",
        "Readmissiona",
        "Queueb",
    ]
    assert [g.job_id for g in grants if isinstance(g, Readmission)] == [
        w.job_id for w in waiting[:1000]
    ]


def test_single_class_is_round_robin():
    profile = load("ci")
    assert profile.priority_classes == ["standard"]
    tenants = {tenant: profile.tenants["acme"] for tenant in ("acme", "beta", "gamma")}
    caps = Caps.of(profile.model_copy(update={"tenants": tenants}))
    assert (caps.open, caps.global_, caps.reserve_interval) == (2000, 1000, 5)
    waiting = [queue(tenant) for tenant in tenants]
    grants = plan(Snapshot({}, 0), caps, profile.priority_classes, waiting)
    assert len(grants) == caps.global_
    assert [g.tenant for g in grants[:9]] == ["acme", "beta", "gamma"] * 3
    resumed = plan(
        Snapshot({}, 0), caps, profile.priority_classes, waiting, turn=Turn(4, {"standard": "beta"})
    )
    assert [g.tenant for g in resumed[:3]] == ["gamma", "acme", "beta"]


def test_policy_caps_of_a_profile_take_a_lower_global_cap_only():
    profile = load("ci")
    assert Caps.of(profile).quotas == {"acme": 1000}
    assert Caps.of(profile).job_class == {"short": 1000, "long": 100}
    assert Caps.of(profile, global_cap=10).global_ == 10
    assert Caps.of(profile, global_cap=5000).global_ == 1000
