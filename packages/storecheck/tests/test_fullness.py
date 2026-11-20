"""A size-capped volume filled to the first refused put, on both products."""

import re
import subprocess

import pytest
from wft_storecheck.checks import contract_gaps
from wft_storecheck.fullness import (
    GAP,
    SEAWEEDFS_SHELL,
    free_outside_s3,
    observe_full_disk,
    vacuum_through,
)
from wft_storecheck.s3 import ALLOWED

pytestmark = pytest.mark.integration

CAP_MIB = 128


ATTEMPTS = 3
STORE_LOG = re.compile(r"compact|vacuum|dir /data |becomes|insufficient", re.IGNORECASE)


def free_kib(prefix: list[str]) -> int:
    """The free space of the data volume as the kernel inside the store reports it."""
    df = subprocess.run([*prefix, "df", "-k", "/data"], capture_output=True, text=True, check=True)
    return int(df.stdout.splitlines()[1].split()[3])


def vacuum_noting_room(prefix: list[str], room: list[int], said: list[str]):
    """The vacuum of SeaweedFS, noting the free space it had to write the compacted volume into."""
    vacuum = vacuum_through(prefix)

    def recover(store) -> None:
        room.append(free_kib(prefix))
        said.append(vacuum(store))

    return recover


def store_state(started) -> str:
    """What SeaweedFS reports about its disk and volumes, for a recovery that did not come back."""
    prefix = started.exec_prefix()
    volumes = subprocess.run(
        [*prefix, *SEAWEEDFS_SHELL], input="volume.list\n", text=True, capture_output=True
    ).stdout
    logs = subprocess.run(["docker", "logs", started.container_id], capture_output=True, text=True)
    lines = [line for line in (logs.stdout + logs.stderr).splitlines() if STORE_LOG.search(line)]
    return "\n".join([f"free {free_kib(prefix)} KiB", volumes, *lines[-40:]])


@pytest.fixture(scope="module")
def runs(start_capped):
    """The whole full-disk sequence of each product, so the cases below read one run each.

    SeaweedFS learns of its free space from a heartbeat, so a fast fill can overshoot into the
    room that its metadata needs, and then even a delete is refused.
    A fill that leaves no free byte at all also leaves the vacuum no room to write the compacted
    volume into, so it reclaims nothing.
    Neither run says anything about the recovery, so the sequence starts again on a fresh volume.
    """
    seen = {}
    for product in ("seaweedfs", "rustfs"):
        for _ in range(ATTEMPTS):
            room, said = [], []
            with start_capped(product, CAP_MIB) as started:
                prefix = started.exec_prefix()
                recover = (
                    vacuum_noting_room(prefix, room, said)
                    if product == "seaweedfs"
                    else free_outside_s3(prefix)
                )
                seen[product] = observe_full_disk(started.store(), product, recover)
                if product == "seaweedfs" and seen[product].put_after_recovery != ALLOWED:
                    seen["seaweedfs-state"] = "\n".join([*said[-1:], store_state(started)])
            if product == "rustfs" or (seen[product].delete == ALLOWED and room[:1] != [0]):
                break
    return seen


@pytest.mark.parametrize("product", ["seaweedfs", "rustfs"])
def test_disk_full_refuses_with_500_and_keeps_survivors(runs, product):
    run = runs[product]
    assert run.refusal == ("InternalError", 500)
    assert run.second_refusal == ("InternalError", 500)
    assert 20 < len(run.written) <= CAP_MIB // 4
    assert run.survivors_exact == len(run.written)


def test_batch_delete_works_while_full_on_seaweedfs(runs):
    assert runs["seaweedfs"].delete == ALLOWED


def test_recover_needs_vacuum_on_seaweedfs(runs):
    run = runs["seaweedfs"]
    assert run.put_after_delete == "refused"
    assert run.put_after_recovery == ALLOWED, runs.get("seaweedfs-state")


def test_delete_fails_while_full_on_rustfs(runs):
    run = runs["rustfs"]
    assert run.delete != ALLOWED
    assert "reaper" in contract_gaps("rustfs")[GAP]
    assert run.put_after_delete == "refused"
    assert run.put_after_recovery == ALLOWED
