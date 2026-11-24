"""Spike S4b: the five S4 checks on Python 3.13 and the measurements S4 never ran.

    python s4b.py checks --runs 3 --out DIR
    python s4b.py measure --out DIR [--only 6,7,...]

Every number is printed as a `figure <name> <value>` line. `checks` exits 1 when any assertion of
any run fails, and `measure` exits 1 when a measurement step raised.
"""

import argparse
import json
import platform
import sys
import threading
import time
import urllib.request
from pathlib import Path

import amqp
import broker
import proton
from broker import DLQ, INTAKE, TINY

ROOT = Path(__file__).resolve().parents[5]
GOLDEN = (ROOT / "contracts/events/vectors/golden/job.accepted/1.0.json").read_bytes()
POISON = (ROOT / "contracts/events/vectors/poison/not-json.json").read_bytes()
Q = INTAKE[0]
PROPERTIES, APP_PROPERTIES, DATA, ANNOTATIONS = 0x73, 0x74, 0x75, 0x72


def figure(name, value):
    print(f"figure {name} {value}", flush=True)


class Tally:
    def __init__(self, prefix):
        self.prefix, self.passed, self.failed = prefix, 0, []

    def check(self, ok, what):
        if ok:
            self.passed += 1
        else:
            self.failed.append(what)
            print(f"FAIL {self.prefix} {what}", flush=True)
        return ok


def users(b):
    def c(u):
        return amqp.connect(b.address, u, b.passwords[u])

    return c("dispatch"), c("admission"), c("operator")


def bare(raw: bytes) -> bytes:
    """The header, properties, application-properties and data sections, without annotations."""
    s = amqp.sections(raw)
    return b"".join(s[k] for k in (0x70, PROPERTIES, APP_PROPERTIES, DATA) if k in s)


def death_or_dsy(annotations) -> list[str]:
    return sorted(
        str(k) for k in annotations if "death" in str(k) or str(k).startswith("x-opt-dsy")
    )


def empty_queue(conn, queue_name):
    c = conn.consumer(queue_name)
    for m in c.drain(10000, 5):
        m.ack()
    c.close()


# The five S4 checks.


def check1(t, b, pub, adm, op):
    t.check(pub.publish(Q, GOLDEN) == "accepted", "1 routable publish accepted")
    t.check(pub.publish("intake.nobody.standard", GOLDEN) == "released", "1 unroutable released")
    outcomes = [pub.publish(TINY, GOLDEN) for _ in range(8)]
    accepted = outcomes.count("accepted")
    figure(f"{t.prefix}.check1.tiny_outcomes", ",".join(outcomes))
    t.check(outcomes[:3] == ["accepted"] * 3, "1 first three accepted on a queue of three")
    t.check(outcomes[-1] == "rejected", "1 full queue rejected")
    t.check(accepted + outcomes.count("rejected") == 8, "1 every publish accepted or rejected")
    # A missing queue is a refused attach (S4 finding 6), on a connection of its own. A missing
    # exchange that the user may not write to is a permission refusal instead, recorded only.
    probe = amqp.connect(b.address, "admission", b.passwords["admission"])
    try:
        probe.consumer("intake.nobody.standard").pull(1, 3)
        t.check(False, "1 missing queue refused")
    except amqp.TopologyError as exc:
        figure(f"{t.prefix}.check1.missing_queue", f"TopologyError:{exc}".replace(" ", "_"))
        t.check(True, "1 missing queue refused")
    t.check(probe.dead is None, "1 connection usable after a refused attach")
    probe.close()
    probe = amqp.connect(b.address, "operator", b.passwords["operator"])
    try:
        probe.publish("x", GOLDEN, timeout_s=3, address="/exchanges/dsy.missing/x")
    except Exception as exc:
        figure(
            f"{t.prefix}.check1.unpermitted_exchange",
            f"{type(exc).__name__}:{exc}".replace(" ", "_"),
        )
    if not probe.dead:
        probe.close()
    empty_queue(adm, Q)
    c = adm.consumer(TINY)
    got = c.drain(100, 5)
    t.check(len(got) == accepted, "1 tiny queue holds what was accepted")
    for m in got:
        m.ack()
    c.close()


def check2(t, b, pub, adm, op):
    for _ in range(3):
        pub.publish(Q, GOLDEN)
    b.wait_counts(Q, 3, 0)
    c = adm.consumer(Q)
    one = c.pull(1, 5)
    t.check(len(one) == 1, "2 credit 1 gives 1")
    t.check(b.wait_counts(Q, 2, 1) == (2, 1), "2 broker ready 2 unacked 1 after credit 1")
    time.sleep(2)
    t.check(not c.inbox, "2 nothing extra after 2 s")
    two = c.pull(2, 5)
    t.check(len(two) == 2, "2 credit 2 gives 2")
    time.sleep(2)
    t.check(not c.inbox, "2 nothing extra after credit 2")
    for m in one + two:
        m.ack()
    start = time.monotonic()
    t.check(c.drain(5, 5) == [], "2 drain on empty returns nothing")
    took = time.monotonic() - start
    figure(f"{t.prefix}.check2.drain_empty_s", f"{took:.3f}")
    t.check(took < 1, "2 drain on empty answers within 1 s")
    c.close()


def check3(t, b, pub, adm, op):
    pub.publish(Q, GOLDEN)
    c = adm.consumer(Q)
    counts = []
    for _ in range(4):
        m = c.pull(1, 5)[0]
        counts.append(m.delivery_count)
        m.retry()
    m = c.pull(1, 5)[0]
    m.ack()
    figure(f"{t.prefix}.check3.retry_counts", ",".join(map(str, counts)))
    t.check(counts == [0, 1, 2, 3], "3 retry counts 0,1,2,3")
    pub.publish(Q, GOLDEN)
    control = []
    for _ in range(4):
        m = c.pull(1, 5)[0]
        control.append(m.delivery_count)
        m.release()
    c.pull(1, 5)[0].ack()
    figure(f"{t.prefix}.check3.release_counts", ",".join(map(str, control)))
    t.check(control == [0, 0, 0, 0], "3 release counts 0,0,0,0")
    c.close()


def check4(t, b, pub, adm, op):
    pub.publish(Q, GOLDEN)
    c = adm.consumer(Q)
    counts = []
    for _ in range(6):
        m = c.pull(1, 5)
        if not m:
            break
        counts.append(m[0].delivery_count)
        m[0].retry()
    figure(f"{t.prefix}.check4.counts", ",".join(map(str, counts)))
    t.check(counts == [0, 1, 2, 3, 4, 5], "4 six deliveries counted 0 to 5")
    t.check(c.drain(1, 3) == [], "4 no seventh delivery")
    d = op.consumer(DLQ)
    dead = d.pull(1, 10)
    t.check(len(dead) == 1, "4 sixth failure dead-lettered")
    if dead:
        reason = dead[0].annotations.get("x-last-death-reason")
        figure(f"{t.prefix}.check4.limit_reason", reason)
        t.check(reason == "delivery_limit", "4 broker reason delivery_limit")
        t.check("x-opt-dsy-reason" not in dead[0].annotations, "4 no application reason")
        dead[0].ack()
    # A sixth delivery that is acked never dead-letters.
    pub.publish(Q, GOLDEN)
    for _ in range(5):
        c.pull(1, 5)[0].retry()
    last = c.pull(1, 5)[0]
    t.check(last.delivery_count == 5, "4 sixth delivery carries count 5")
    last.ack()
    t.check(d.drain(1, 3) == [], "4 ack on the sixth leaves the dead-letter queue empty")
    # The reason travels in the same settle.
    pub.publish(Q, POISON)
    c.pull(1, 5)[0].dead_letter("MALFORMED", "not-json: body is not JSON")
    dead = d.pull(1, 10)
    t.check(len(dead) == 1, "4 dead_letter moves the message at once")
    if dead:
        a = dead[0].annotations
        t.check(a.get("x-opt-dsy-reason") == "MALFORMED", "4 reason annotation set")
        t.check(
            a.get("x-opt-dsy-detail") == "not-json: body is not JSON", "4 detail annotation set"
        )
        t.check(a.get("x-last-death-reason") == "rejected", "4 broker reason rejected")
        figure(f"{t.prefix}.check4.dead_letter_annotations", ",".join(sorted(map(str, a))))
        dead[0].ack()
    c.close()
    d.close()


def check5(t, b, pub, adm, op):
    sent = amqp.Message(body=GOLDEN, inferred=True, content_type=amqp.CONTENT_TYPE, durable=True)
    sent_raw = amqp.sections(sent.encode())
    pub.publish(Q, GOLDEN)
    c = adm.consumer(Q)
    at_intake = c.pull(1, 5)[0]
    s_in = amqp.sections(at_intake.raw)
    for code, name in ((PROPERTIES, "properties"), (DATA, "data")):
        t.check(s_in.get(code) == sent_raw.get(code), f"5 {name} identical at intake")
    t.check(APP_PROPERTIES not in s_in, "5 no application-properties at intake")
    t.check(at_intake.body == GOLDEN, "5 body identical at intake")
    at_intake.dead_letter("ADMISSION_FAILED", "no-row: spike")
    d = op.consumer(DLQ)
    dead = d.pull(1, 10)[0]
    s_dl = amqp.sections(dead.raw)
    for code, name in ((PROPERTIES, "properties"), (DATA, "data")):
        t.check(
            s_dl.get(code) == sent_raw.get(code), f"5 {name} identical in the dead-letter queue"
        )
    t.check(dead.annotations.get("x-first-death-queue") == Q, "5 first-death queue recorded")
    op.publish_raw(Q, bare(dead.raw))
    dead.ack()
    replayed = c.pull(1, 10)[0]
    s_re = amqp.sections(replayed.raw)
    for code, name in ((PROPERTIES, "properties"), (DATA, "data")):
        t.check(s_re.get(code) == sent_raw.get(code), f"5 {name} identical after replay")
    t.check(replayed.body == GOLDEN, "5 body identical after replay")
    t.check(death_or_dsy(replayed.annotations) == [], "5 no death or dsy annotation after replay")
    figure(f"{t.prefix}.check5.bytes", len(replayed.body))
    figure(
        f"{t.prefix}.check5.replay_annotations", ",".join(sorted(map(str, replayed.annotations)))
    )
    # Negative control: resending the dead letter whole carries its stale annotations, and a
    # changed body byte fails the identity check.
    replayed.dead_letter("ADMISSION_FAILED", "no-row: control")
    dead = d.pull(1, 10)[0]
    op.publish_raw(Q, dead.raw)
    dead.ack()
    stale = c.pull(1, 10)[0]
    t.check(death_or_dsy(stale.annotations) != [], "5 negative control carries stale annotations")
    figure(f"{t.prefix}.check5.control_annotations", ",".join(death_or_dsy(stale.annotations)))
    flipped = bytearray(stale.body)
    flipped[-2] ^= 1
    t.check(bytes(flipped) != GOLDEN, "5 negative control body check can fail")
    stale.ack()
    c.close()
    d.close()


CHECKS = [check1, check2, check3, check4, check5]


def run_checks(args):
    label = f"proton{'.'.join(map(str, proton.VERSION))}"
    figure(f"{label}.python", platform.python_version())
    figure(f"{label}.proton", ".".join(map(str, proton.VERSION)))
    failed = 0
    for n in range(1, args.runs + 1):
        t = Tally(f"{label}.run{n}")
        with broker.running(args.out) as b:
            pub, adm, op = users(b)
            for check in CHECKS:
                try:
                    check(t, b, pub, adm, op)
                except Exception as exc:
                    t.check(False, f"{check.__name__} raised {type(exc).__name__}: {exc}")
            for conn in (pub, adm, op):
                conn.close()
        figure(f"{t.prefix}.assertions", f"{t.passed}/{t.passed + len(t.failed)}")
        failed += len(t.failed)
    figure(f"{label}.verdict", "pass" if failed == 0 else "fail")
    return 1 if failed else 0


# The measurements S4 never ran.


def connection_pid(b, user):
    rows = b.ctl("list_connections", "pid", "user", "--no-table-headers")
    return [line.split()[0] for line in rows.splitlines() if line.split()[1] == user]


def held_then(b, cause, retries_first=0):
    """Holds a delivery unsettled while `cause` drops it, then reads where it went and its count."""
    pub, adm, op = users(b)
    pub.publish(Q, GOLDEN)
    c = adm.consumer(Q)
    for _ in range(retries_first):
        c.pull(1, 5)[0].retry()
    held = c.pull(1, 5)[0]
    before = held.delivery_count
    error = "none"
    if cause == "close_connection":
        for pid in connection_pid(b, "admission"):
            b.ctl("close_connection", pid, "s4b")
        time.sleep(1)
        try:
            c.pull(1, 2)
        except amqp.BrokerUnavailable as exc:
            error = f"BrokerUnavailable:{exc}"
    elif cause == "client_close":
        adm.close()
    elif cause == "restart":
        b.restart()
        try:
            adm.publish(Q, GOLDEN, timeout_s=2)
        except (amqp.BrokerUnavailable, amqp.PublishTimeout) as exc:
            error = f"{type(exc).__name__}:{exc}"
        pub, op = (amqp.connect(b.address, u, b.passwords[u]) for u in ("dispatch", "operator"))
    adm2 = amqp.connect(b.address, "admission", b.passwords["admission"])
    c2 = adm2.consumer(Q)
    again = c2.drain(1, 10)
    d = op.consumer(DLQ)
    dead = d.drain(1, 10)
    where = "intake" if again else ("dlq" if dead else "gone")
    after = again[0].delivery_count if again else None
    reason = dead[0].annotations.get("x-last-death-reason") if dead else None
    for m in again + dead:
        m.ack()
    for conn in (pub, adm2, op):
        conn.close()
    return before, after, where, reason, error.replace(" ", "_")


def m6(b):
    for cause in ("close_connection", "client_close", "restart"):
        for retries in (0, 5):
            before, after, where, reason, error = held_then(b, cause, retries)
            k = f"m6.{cause}.held_count{retries}"
            figure(f"{k}.count_before", before)
            figure(f"{k}.count_after", after)
            figure(f"{k}.went_to", where)
            figure(f"{k}.dead_letter_reason", reason)
            figure(f"{k}.error_on_old_connection", error)


def m7(b):
    out = b.ctl(
        "set_policy",
        "--apply-to",
        "quorum_queues",
        "dsy-intake",
        broker.INTAKE_PATTERN,
        json.dumps(
            {
                **broker.INTAKE_POLICY,
                "delayed-retry-type": "all",
                "delayed-retry-min": 1000,
                "delayed-retry-max": 30000,
            }
        ),
    )
    figure("m7.set_policy", out.strip().replace(" ", "_") or "ok")
    figure(
        "m7.effective",
        b.ctl("list_queues", "name", "effective_policy_definition", "--no-table-headers")
        .split(Q, 1)[1]
        .splitlines()[0]
        .strip()
        .replace(" ", ""),
    )
    for kind in ("all", "failed", "returned", "disabled"):
        b.ctl(
            "set_policy",
            "--apply-to",
            "quorum_queues",
            "dsy-intake",
            broker.INTAKE_PATTERN,
            json.dumps(
                {
                    **broker.INTAKE_POLICY,
                    "delayed-retry-type": kind,
                    "delayed-retry-min": 1000,
                    "delayed-retry-max": 30000,
                }
            ),
        )
        time.sleep(1)
        pub, adm, op = users(b)
        pub.publish(Q, GOLDEN)
        c = adm.consumer(Q)
        counts, gaps, last = [], [], None
        for _ in range(6):
            got = c.pull(1, 60)
            if not got:
                break
            now = time.monotonic()
            if last is not None:
                gaps.append(f"{now - last:.1f}")
            counts.append(got[0].delivery_count)
            got[0].retry()
            last = time.monotonic()
        d = op.consumer(DLQ)
        dead = d.pull(1, 20)  # at-least-once dead-lettering is asynchronous, so wait for it
        figure(f"m7.{kind}.intake_after", ",".join(map(str, b.queue_counts()[Q])))
        figure(f"m7.{kind}.retry_counts", ",".join(map(str, counts)))
        figure(f"m7.{kind}.gaps_s", ",".join(gaps) or "-")
        figure(f"m7.{kind}.dead_lettered", len(dead))
        for m in dead:
            figure(f"m7.{kind}.reason", m.annotations.get("x-last-death-reason"))
            m.ack()
        # An uncounted release under the same policy.
        pub.publish(Q, GOLDEN)
        m = c.pull(1, 10)[0]
        m.release()
        t0 = time.monotonic()
        again = c.pull(1, 60)
        figure(f"m7.{kind}.release_gap_s", f"{time.monotonic() - t0:.1f}")
        figure(f"m7.{kind}.release_count", again[0].delivery_count if again else None)
        for x in again:
            x.ack()
        for conn in (pub, adm, op):
            conn.close()
    b.ctl(
        "set_policy",
        "--apply-to",
        "quorum_queues",
        "dsy-intake",
        broker.INTAKE_PATTERN,
        json.dumps(broker.INTAKE_POLICY),
    )


def m8(b):
    figure(
        "m8.users",
        ",".join(
            sorted(
                line.split()[0] for line in b.ctl("list_users", "--no-table-headers").splitlines()
            )
        ),
    )
    for line in b.ctl("list_permissions", "--no-table-headers").splitlines():
        u, c, w, r = line.split("\t")
        figure(f"m8.permissions.{u}", f"configure={c};write={w};read={r}")
    for line in b.ctl("list_policies", "--no-table-headers").splitlines():
        parts = line.split("\t")
        figure(f"m8.policy.{parts[1]}", "".join(parts[2:]).replace(" ", ""))
    for line in b.ctl(
        "list_queues", "name", "type", "policy", "arguments", "--no-table-headers"
    ).splitlines():
        parts = line.split("\t")
        figure(f"m8.queue.{parts[0]}", ";".join(p.replace(" ", "") for p in parts[1:]))
    for line in b.ctl(
        "list_exchanges", "name", "type", "durable", "--no-table-headers"
    ).splitlines():
        name, kind, durable = (line.split("\t") + ["", "", ""])[:3]
        if name.startswith("dsy"):
            figure(f"m8.exchange.{name}", f"{kind};durable={durable}")
    figure(
        "m8.bindings",
        sum(
            1
            for line in b.ctl("list_bindings", "source_name", "--no-table-headers").splitlines()
            if line.startswith("dsy")
        ),
    )
    try:
        amqp.connect(b.address, "guest", "guest").close()
        figure("m8.guest_login", "accepted")
    except amqp.BrokerUnavailable as exc:
        figure("m8.guest_login", f"refused:{exc}".replace(" ", "_"))
    # Each user is held to its permissions.
    pub, adm, op = users(b)
    for who, conn, fn in (
        ("dispatch_consume", pub, lambda c: c.consumer(Q).pull(1, 2)),
        ("admission_publish", adm, lambda c: c.publish(Q, GOLDEN, timeout_s=3)),
        ("operator_publish_dlx", op, lambda c: c.publish("x", GOLDEN, 3, "/exchanges/dsy.dlx/x")),
    ):
        try:
            figure(f"m8.refused.{who}", f"allowed:{fn(conn)}")
        except Exception as exc:
            figure(f"m8.refused.{who}", f"{type(exc).__name__}:{exc}".replace(" ", "_"))
    for conn in (pub, adm, op):
        if not conn.dead:
            conn.close()


def m9(b):
    figure(
        "m9.dlq_effective",
        b.ctl("list_queues", "name", "effective_policy_definition", "--no-table-headers")
        .split(DLQ, 1)[1]
        .splitlines()[0]
        .strip()
        .replace(" ", ""),
    )
    pub, adm, op = users(b)
    c = adm.consumer(Q)
    for i in range(5):
        body = GOLDEN.replace(b"sub-1", f"sub-{i}".encode())
        pub.publish(Q, body)
        c.pull(1, 5)[0].dead_letter("ADMISSION_FAILED", f"no-row: {i}")
    b.wait_counts(DLQ, 5, 0)
    d = op.consumer(DLQ)
    orders, counts = [], []
    for _ in range(25):
        got = d.pull(5, 10)
        orders.append("".join(m.annotations["x-opt-dsy-detail"][-1] for m in got))
        counts.append(
            ",".join(
                f"{m.delivery_count}/{m.annotations.get('x-delivery-count')}"
                f"/{m.annotations.get('x-acquired-count')}"
                for m in got
            )
        )
        for m in got:
            m.released()
    figure("m9.browse_orders_distinct", ",".join(sorted(set(orders))))
    figure("m9.browse_passes", len(orders))
    figure("m9.browse_counts_first_pass", counts[0])
    figure("m9.browse_counts_last_pass", counts[-1])
    figure("m9.dlq_after_browse", ",".join(map(str, b.wait_counts(DLQ, 5, 0))))
    # A browse that holds the head and settles it after the rest.
    got = d.pull(5, 10)
    for m in got[1:] + got[:1]:
        m.released()
    again = d.pull(5, 10)
    figure(
        "m9.order_after_out_of_order_release",
        "".join(m.annotations["x-opt-dsy-detail"][-1] for m in again),
    )
    for m in again:
        m.released()
    # The delivery limit is off: thirty counted failures leave the message in place.
    counts = []
    for _ in range(30):
        got = d.pull(1, 10)
        if not got:
            break
        a = got[0].annotations
        counts.append(
            f"{a['x-opt-dsy-detail'][-1]}:{got[0].delivery_count}"
            f"/{a.get('x-delivery-count')}/{a.get('x-acquired-count')}"
        )
        got[0].retry()
    figure("m9.dlq_retries_survived", len(counts))
    figure("m9.dlq_retry_counts", ",".join(counts))
    figure("m9.dlq_after_retries", ",".join(map(str, b.wait_counts(DLQ, 5, 0))))
    for m in d.drain(10, 10):
        m.ack()
    for conn in (pub, adm, op):
        conn.close()


def m10(b):
    P = f"m10.{b.limit_mib}"
    cg = b.cgroup()
    figure(f"{P}.watermark_mib", b.watermark_mib)
    figure(f"{P}.memory_max", (cg / "memory.max").read_text().strip())
    figure(f"{P}.boot_peak_mib", f"{int((cg / 'memory.peak').read_text()) / 1048576:.1f}")
    samples, broker_rss, alarms, stop = [], [], set(), threading.Event()
    phase = ["fill"]

    def sample():
        while not stop.is_set():
            try:
                stat = dict(line.split() for line in (cg / "memory.stat").read_text().splitlines())
                current = int((cg / "memory.current").read_text())
                samples.append((phase[0], (current - int(stat["inactive_file"])) / 1048576))
            except OSError:  # the scope is recreated while the container restarts
                pass
            time.sleep(0.05)

    def scrape():
        # The broker's own view of its memory, which the watermark compares, and its alarm.
        url = b.metrics.removesuffix("/detailed")
        while not stop.is_set():
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    for line in r.read().decode().splitlines():
                        if line.startswith("rabbitmq_process_resident_memory_bytes"):
                            broker_rss.append((phase[0], float(line.split()[-1]) / 1048576))
                        elif line.startswith("rabbitmq_alarms_memory_used_watermark"):
                            alarms.add((phase[0], line.split()[-1]))
            except OSError:
                pass
            time.sleep(0.5)

    threading.Thread(target=sample, daemon=True).start()
    threading.Thread(target=scrape, daemon=True).start()
    pub, adm, op = users(b)
    outcomes: dict[str, int] = {}
    t0 = time.monotonic()

    def fill(q, n):
        for _ in range(n):
            try:
                o = pub.publish(q, GOLDEN, timeout_s=5)
            except amqp.PublishTimeout:
                o = "timeout"
            outcomes[o] = outcomes.get(o, 0) + 1

    fill(Q, 1000)
    c = adm.consumer(Q)
    moved = 0
    while moved < 1000:
        got = c.pull(100, 10)
        for m in got:
            m.dead_letter("ADMISSION_FAILED", "no-row: m10")
        moved += len(got)
        if not got:
            break
    for q in INTAKE:
        fill(q, 1000)
    figure(f"{P}.fill_s", f"{time.monotonic() - t0:.1f}")
    time.sleep(10)
    for conn in (pub, adm, op):
        conn.close()
    # Recovery with every queue full, the other peak a broker of this size meets.
    phase[0] = "restart"
    t0 = time.monotonic()
    b.restart()
    figure(f"{P}.restart_ready_s", f"{time.monotonic() - t0:.1f}")
    time.sleep(10)
    stop.set()
    figure(f"{P}.publish_outcomes", ",".join(f"{k}={v}" for k, v in sorted(outcomes.items())))
    figure(f"{P}.dead_lettered", moved)
    for name, (ready, unacked) in sorted(b.queue_counts().items()):
        figure(f"{P}.queue.{name}", f"ready={ready};unacked={unacked}")
    for ph in ("fill", "restart"):
        ws = [v for p, v in samples if p == ph]
        rss = [v for p, v in broker_rss if p == ph]
        figure(f"{P}.{ph}.peak_working_set_mib", f"{max(ws):.1f}" if ws else None)
        figure(f"{P}.{ph}.peak_broker_rss_mib", f"{max(rss):.1f}" if rss else None)
        figure(
            f"{P}.{ph}.watermark_alarm_values", ",".join(sorted(v for p, v in alarms if p == ph))
        )
    figure(f"{P}.memory_peak_mib", f"{int((cg / 'memory.peak').read_text()) / 1048576:.1f}")
    events = dict(line.split() for line in (cg / "memory.events").read_text().splitlines())
    figure(f"{P}.oom_kill", events.get("oom_kill"))
    figure(
        f"{P}.alarms",
        b.exec("rabbitmq-diagnostics", "-q", "alarms").strip().replace("\n", "|").replace(" ", "_")
        or "none",
    )
    status = b.exec("rabbitmq-diagnostics", "-q", "status")
    for line in status.splitlines():
        if line.startswith(("Total memory used", "Calculation strategy", "Memory high watermark")):
            key, _, value = line.partition(":")
            figure(
                f"{P}.status.{key.strip().replace(' ', '_').lower()}",
                value.strip().replace(" ", "_"),
            )
    breakdown = b.exec("rabbitmq-diagnostics", "-q", "memory_breakdown", "--unit", "mb")
    figure(
        f"{P}.memory_breakdown_top",
        "|".join(line.strip().replace(" ", "") for line in breakdown.splitlines()[:6]),
    )


def m11(b):
    pub, adm, op = users(b)
    pub.publish(Q, GOLDEN)
    pub.publish(Q, GOLDEN)
    c = adm.consumer(Q)
    held = c.pull(1, 5)
    time.sleep(8)  # the detailed endpoint lags
    for family in ("queue_coarse_metrics", "queue_consumer_count"):
        names = sorted(
            {
                line.split("{")[0]
                for line in b.scrape(f"family={family}").splitlines()
                if line.startswith("rabbitmq")
            }
        )
        figure(f"m11.series.{family}", ",".join(names))
    text = b.scrape("family=queue_coarse_metrics&family=queue_consumer_count")
    for line in text.splitlines():
        if f'queue="{Q}"' in line or f'queue="{DLQ}"' in line:
            figure("m11.line", line.replace(" ", "_"))
    for m in held:
        m.ack()
    c.drain(5, 5)[0].ack()
    for conn in (pub, adm, op):
        conn.close()


def m13(b):
    pub, adm, op = users(b)
    for i in "ABC":
        pub.publish(Q, GOLDEN.replace(b"sub-1", f"sub-{i}".encode()))
    c = adm.consumer(Q)
    first = c.pull(1, 5)[0]
    first.release()
    got = c.pull(3, 10)
    figure(
        "m13.order_after_release",
        "".join(json.loads(m.body)["data"]["submission_key"][-1] for m in got),
    )
    figure("m13.counts_after_release", ",".join(str(m.delivery_count) for m in got))
    for m in got:
        m.ack()
    # After a counted retry, for comparison.
    for i in "ABC":
        pub.publish(Q, GOLDEN.replace(b"sub-1", f"sub-{i}".encode()))
    c.pull(1, 5)[0].retry()
    got = c.pull(3, 10)
    figure(
        "m13.order_after_retry",
        "".join(json.loads(m.body)["data"]["submission_key"][-1] for m in got),
    )
    figure("m13.counts_after_retry", ",".join(str(m.delivery_count) for m in got))
    for m in got:
        m.ack()
    # Two held deliveries settled back in the opposite order.
    for i in "ABCD":
        pub.publish(Q, GOLDEN.replace(b"sub-1", f"sub-{i}".encode()))
    a, b_ = c.pull(2, 5)
    b_.release()
    a.release()
    got = c.pull(4, 10)
    figure(
        "m13.order_after_release_b_then_a",
        "".join(json.loads(m.body)["data"]["submission_key"][-1] for m in got),
    )
    for m in got:
        m.ack()
    for conn in (pub, adm, op):
        conn.close()


MEASURES = {"8": m8, "11": m11, "13": m13, "9": m9, "7": m7, "6": m6, "10": m10}


def run_measures(args):
    failed = 0
    for key in [k for k in MEASURES if k in args.only.split(",")]:
        # The memory check runs at the limit of D14 and at each 64 MiB step above it, with the
        # watermark at 0.6 of the limit.
        limits = (
            [(int(m), round(int(m) * 0.6)) for m in args.m10_limits.split(",")]
            if key == "10"
            else [(1024, 154)]
        )
        for limit, watermark in limits:
            try:
                with broker.running(args.out, f"{limit}m", watermark) as b:
                    b.watermark_mib, b.limit_mib = watermark, limit
                    MEASURES[key](b)
            except Exception as exc:
                figure(f"m{key}.error", f"{type(exc).__name__}:{exc}".replace(" ", "_")[:400])
                failed += 1
    return 1 if failed else 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["checks", "measure"])
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--only", default=",".join(MEASURES))
    p.add_argument("--m10-limits", default="256,320")
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    sys.exit(run_checks(args) if args.command == "checks" else run_measures(args))


if __name__ == "__main__":
    main()
