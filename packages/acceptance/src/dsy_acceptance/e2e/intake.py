"""The logic of the intake negatives: the same submission twice (N3), a malformed and an
unsupported-major event on the intake queue (N4), and a cancel before admission (N5).

Every function takes the clients it reads and writes through as arguments, so the verdicts run
against fakes. A scenario that changes shared state, the dead-letter queue or the admission
replicas, restores it in a `finally`.
"""

import contextlib
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass

from dsy_clusterkit import state_dir
from dsy_events.errors import SchemaInvalid, UnsupportedMajor
from temporalio.service import RPCError, RPCStatusCode

from . import wait

ADMISSION = "deployment/admission"
# How long admission takes to stop, and to be ready again.
UP_S = 120.0
# The poison events N4 publishes, and the dead letter reason each must carry.
POISON = {"not-json": SchemaInvalid.dlq_reason, "wrong-major": UnsupportedMajor.dlq_reason}


def duplicate_submission(
    submit: Callable[[], list[dict]],
    rows: Callable[[], list[str]],
    workflows: Callable[[str], int],
) -> str:
    """N3: `submit` sends the same one-entry batch each time it is called and returns the answer
    lines, `rows` the job ids that hold the entry's key, `workflows` how many workflow executions
    a job id has. Returns the job id."""
    first, again = ([a for a in submit() if "outcome" in a] for _ in range(2))
    outcomes = [[a["outcome"] for a in answers] for answers in (first, again)]
    if outcomes != [["accepted"], ["duplicate"]]:
        raise AssertionError(f"the answers were {outcomes}, not [['accepted'], ['duplicate']]")
    job_id = first[0]["job_id"]
    if again[0]["job_id"] != job_id:
        raise AssertionError(f"the resend answered job {again[0]['job_id']}, not {job_id}")
    held = rows()
    if held != [job_id]:
        raise AssertionError(f"the key holds {len(held)} job rows {held}, not one: {job_id}")
    count = workflows(job_id)
    if count != 1:
        raise AssertionError(f"job {job_id} has {count} workflows, not one")
    return job_id


def rows_for_key(conn, tenant: str, key: str) -> list[str]:
    """The ids of the accepted jobs of the tenant that hold the submission key."""
    found = conn.execute(
        "SELECT job_id::text FROM accepted_job WHERE tenant = %s AND submission_key = %s",
        (tenant, key),
    ).fetchall()
    return [row[0] for row in found]


@dataclass(frozen=True)
class Letter:
    reason: str
    queue: str


def parse_letters(listing: str) -> list[Letter]:
    """The dead letters of a `dlq list` run: one line each of position, reason, detail token, job,
    tenant, class, first-death queue and death time, before the `count` lines."""
    letters = []
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) >= 7 and fields[0].isdigit():
            letters.append(Letter(fields[1], fields[6]))
    return letters


DeadLetters = Callable[..., str]


def dead_letter_tool(address: str) -> DeadLetters:
    """Runs `python -m dsy_broker.dlq <command> ...` as the operator against the broker at
    `address` and returns its standard output."""

    def run(*args: str) -> str:
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "dsy_broker.dlq",
                *args[:1],
                "--address",
                address,
                "--credentials-dir",
                str(state_dir() / "rabbitmq"),
                *args[1:],
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if done.returncode != 0:
            raise RuntimeError(f"dsy_broker.dlq {args[0]} exited {done.returncode}: {done.stderr}")
        return done.stdout

    return run


def malformed_intake(
    dlq: DeadLetters,
    publish: Callable[[bytes], bool],
    poison: dict[str, bytes],
    submit: Callable[[], str],
    ended: Callable[[str], str],
    timeout_s: float = 60.0,
) -> str:
    """N4: `submit` starts another job and returns its id, `publish` puts one body on the intake
    queue and says whether the broker accepted it, `ended` returns the job's final status, and
    `poison` holds the bodies by the names of POISON.

    The queue must be empty to begin with, because the purge at the end must not remove a letter
    this scenario did not cause. The purge runs in a `finally`."""
    held = parse_letters(dlq("list"))
    if held:
        raise AssertionError(
            f"the dead-letter queue holds {len(held)} letters before N4, which its purge would "
            "remove, so purge them first"
        )
    try:
        job_id = submit()
        for name, body in poison.items():
            if not publish(body):
                raise AssertionError(f"the intake queue did not accept {name}")

        def landed() -> bool | None:
            return len(parse_letters(dlq("list"))) >= len(poison) or None

        wait(landed, timeout_s, "the dead letters")
        letters = parse_letters(dlq("list"))
        reasons = sorted(letter.reason for letter in letters)
        if reasons != sorted(POISON[name] for name in poison):
            raise AssertionError(
                f"the dead letters carry reasons {reasons}, not "
                f"{sorted(POISON[name] for name in poison)}"
            )
        for name, body in poison.items():
            shown = dlq("show", "--reason", POISON[name])
            if f"body: {body.decode()}" not in shown:
                raise AssertionError(f"the {POISON[name]} dead letter does not hold {name} as sent")
        try:
            status = ended(job_id)
        except TimeoutError:
            raise AssertionError(f"job {job_id} was held up by the poison events") from None
        if status != "succeeded":
            raise AssertionError(f"job {job_id} ended {status} beside the poison events")
    finally:
        dlq("purge", "--yes")
    purged(dlq)
    return job_id


def purged(dlq: DeadLetters) -> None:
    """Raises when a letter is left in the dead-letter queue."""
    left = parse_letters(dlq("list"))
    if left:
        raise AssertionError(f"the dead-letter queue holds {len(left)} letters after the purge")


@contextlib.contextmanager
def admission_held(kubectl: Callable[..., str], pods: Callable[[], list]) -> Iterator[None]:
    """Admission at no replica for the block, so no job starts, and back at its replica count
    after, also when the block fails. A restore that does not reach the count raises."""
    replicas = max(int(kubectl("get", ADMISSION, "-o", "jsonpath={.spec.replicas}") or 1), 1)
    kubectl("scale", ADMISSION, "--replicas=0")
    try:
        deadline = time.monotonic() + UP_S
        while pods():
            if time.monotonic() > deadline:
                raise AssertionError(f"admission still has a pod {UP_S:.0f} s after its scale to 0")
            time.sleep(1)
        yield
    finally:
        kubectl("scale", ADMISSION, f"--replicas={replicas}")
        kubectl("rollout", "status", ADMISSION, f"--timeout={UP_S:.0f}s")
        now = kubectl("get", ADMISSION, "-o", "jsonpath={.spec.replicas}")
        if now.strip() != str(replicas):
            raise AssertionError(f"admission has {now.strip()} replicas, not the {replicas} it had")


async def workflow_exists(describe: Callable[[str], Awaitable], job_id: str) -> bool:
    """Whether Temporal knows a workflow for the job, read by workflow id and not by search, so a
    lagging index cannot say none."""
    try:
        await describe(job_id)
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    return True


def cancel_before_admission(
    submit: Callable[[], str],
    cancel: Callable[[str], dict],
    status: Callable[[str], str],
    workflow_exists: Callable[[str], bool],
    timeout_s: float = 30.0,
) -> str:
    """N5, inside `admission_held`: the job is accepted, a cancel applies, the job reads
    `cancelled`, and no workflow was ever started for it. Returns the job id."""
    job_id = submit()
    answer = cancel(job_id)
    if answer.get("outcome") != "applied":
        raise AssertionError(f"the cancel of {job_id} answered {answer}, not applied")
    try:
        wait(lambda: status(job_id) == "cancelled" or None, timeout_s, f"{job_id} cancelled")
    except TimeoutError:
        raise AssertionError(f"job {job_id} reads {status(job_id)}, not cancelled") from None
    if workflow_exists(job_id):
        raise AssertionError(f"job {job_id} was cancelled before admission and has a workflow")
    return job_id
