"""The runner with host processes: the group stop, the parent death signal, the live line reader,
the tail, and the OOM fact read from a memory.events file."""

import asyncio
import signal
import subprocess
import sys
import time
from pathlib import Path

from wft_worker.runner import STOP_GRACE_S, Child, Exit

TAIL_BYTES = 65536
# A heartbeat line of the guest contract.
BEAT = 'HEARTBEAT {"progress":0.5,"step":1,"steps":2}'
# Ignores SIGTERM, says so, and sleeps.
IGNORES_TERM = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('ignoring', flush=True); time.sleep(60)"
)
# Stands in for a worker process: starts a sleeping child through the runner, prints its pid and
# sleeps.
WORKER = """\
import asyncio
import sys
from pathlib import Path

from wft_worker.runner import Child


async def main():
    child = await Child.start(["sleep", "60"], Path.cwd())
    print(child._process.pid, flush=True)
    await asyncio.sleep(60)


asyncio.run(main())
"""


def run(body):
    return asyncio.run(asyncio.wait_for(body, 30))


def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def gone(pid: int, within_s: float) -> bool:
    """The process has exited within the time: no /proc entry, or a zombie awaiting its reaping."""
    until = time.monotonic() + within_s
    while time.monotonic() < until:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return True
        if state in ("Z", "X"):
            return True
        time.sleep(0.02)
    return False


def output(child: Child) -> list[str]:
    async def read():
        return [line async for line in child.lines()]

    return read()


def test_stop_kills_grandchildren(tmp_path):
    """A sleeping grandchild, and one that ignores SIGTERM, both die with the group."""
    script = (
        "import subprocess, sys, time\n"
        "soft = subprocess.Popen(['sleep', '60'])\n"
        f"hard = subprocess.Popen([sys.executable, '-c', {IGNORES_TERM!r}])\n"
        "print(soft.pid, hard.pid, flush=True)\n"
        "time.sleep(60)\n"
    )

    async def body():
        child = await Child.start(python(script), tmp_path)
        seen = set()
        async for line in child.lines():
            seen.add(line)
            if len(seen) == 2:
                break
        started = time.monotonic()
        await child.stop()
        exit = await child.wait()
        pids = next(line for line in seen if line != "ignoring")
        return [int(pid) for pid in pids.split()], exit, time.monotonic() - started

    pids, exit, seconds = run(body())
    print(pids, exit, f"{seconds:.2f}s")
    assert exit.signal == signal.SIGTERM
    assert seconds < 1
    assert all(gone(pid, 1) for pid in pids)


def test_stop_kills_a_child_that_ignores_sigterm(tmp_path):
    async def body():
        child = await Child.start(python(IGNORES_TERM), tmp_path)
        assert await anext(child.lines()) == "ignoring"
        started = time.monotonic()
        await child.stop()
        return await child.wait(), time.monotonic() - started

    exit, seconds = run(body())
    print(exit, f"{seconds:.2f}s")
    assert exit == Exit(None, signal.SIGKILL, False)
    assert STOP_GRACE_S <= seconds < STOP_GRACE_S + 1


def test_parent_death_kills_child(tmp_path):
    """A worker process killed with SIGKILL leaves its child dead within 1 s."""
    worker = subprocess.Popen(python(WORKER), cwd=tmp_path, stdout=subprocess.PIPE, text=True)
    try:
        child = int(worker.stdout.readline())
        assert not gone(child, 0.2)
        worker.kill()
        worker.wait()
        assert gone(child, 1)
    finally:
        worker.kill()
        worker.wait()


def test_heartbeat_lines_parsed_live(tmp_path):
    """A beat is seen before the child exits."""
    script = f"import sys, time; print({BEAT!r}, file=sys.stderr, flush=True); time.sleep(60)"

    async def body():
        child = await Child.start(python(script), tmp_path)
        assert child.beat is None
        line = await anext(child.lines())
        seen = child.beat, child._process.returncode
        await child.stop()
        await child.wait()
        return line, seen

    line, (beat, returncode) = run(body())
    assert line == BEAT
    assert beat == {"progress": 0.5, "step": 1, "steps": 2}
    assert returncode is None


def test_lines_and_exit_code(tmp_path):
    """Free output passes as lines, a broken heartbeat is not a beat, and the exit code is kept."""
    script = (
        "import sys\n"
        "print('one', flush=True)\n"
        "print('HEARTBEAT {broken', file=sys.stderr, flush=True)\n"
        "sys.stdout.write('two\\r\\nlast')\n"
        "sys.exit(3)\n"
    )

    async def body():
        child = await Child.start(python(script), tmp_path)
        return await output(child), await child.wait(), child.beat

    lines, exit, beat = run(body())
    assert lines == ["one", "HEARTBEAT {broken", "two", "last"]
    assert beat is None
    assert exit == Exit(3, None, False)


def test_wait_reads_unread_output(tmp_path):
    """A child that writes more than a pipe holds ends when only wait() is called."""
    script = "import sys; sys.stdout.write('x' * (4 * 1024 * 1024))"

    async def body():
        child = await Child.start(python(script), tmp_path)
        return await child.wait(), len(child.tail)

    assert run(body()) == (Exit(0, None, False), TAIL_BYTES)


def test_tail_keeps_last_bytes(tmp_path):
    script = "import sys\nfor i in range(20000): print(f'line {i:05d}')\n"

    async def body():
        child = await Child.start(python(script), tmp_path)
        lines = await output(child)
        await child.wait()
        return lines, child.tail

    lines, tail = run(body())
    expected = "".join(f"line {i:05d}\n" for i in range(20000)).encode()
    assert len(lines) == 20000
    assert tail == expected[-TAIL_BYTES:]


def test_long_line_without_newline(tmp_path):
    """1 MiB of output with no newline ends with a 65,536-byte tail and no error."""
    script = "import sys; sys.stdout.write('ab' * (512 * 1024))"

    async def body():
        child = await Child.start(python(script), tmp_path)
        lines = await output(child)
        return lines, await child.wait(), child.tail

    lines, exit, tail = run(body())
    assert exit == Exit(0, None, False)
    assert len(tail) == TAIL_BYTES
    assert tail == b"ab" * (TAIL_BYTES // 2)
    assert "".join(lines) == "ab" * (512 * 1024)
    assert max(len(line) for line in lines) <= TAIL_BYTES


def events(path: Path, oom_kills: int) -> None:
    path.write_text(f"low 0\nhigh 0\nmax 4\noom 2\noom_kill {oom_kills}\noom_group_kill 0\n")


def test_oom_fact_from_memory_events(tmp_path):
    """A rising oom_kill gives oom=True on a SIGKILL and on a plain non-zero exit alike, and an
    unchanged count gives oom=False."""
    memory_events = tmp_path / "memory.events"
    killed = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    failed = "import sys; sys.exit(1)"

    async def ended(script: str, rise: bool) -> Exit:
        events(memory_events, 3)
        child = await Child.start(python(script), tmp_path, memory_events=memory_events)
        if rise:
            events(memory_events, 4)
        return await child.wait()

    async def body():
        return [
            await ended(killed, rise=True),
            await ended(failed, rise=True),
            await ended(killed, rise=False),
            await ended(failed, rise=False),
            await ended("pass", rise=True),
        ]

    assert run(body()) == [
        Exit(None, signal.SIGKILL, True),
        Exit(1, None, True),
        Exit(None, signal.SIGKILL, False),
        Exit(1, None, False),
        Exit(0, None, False),
    ]


def test_no_memory_events_is_no_oom(tmp_path):
    async def body():
        child = await Child.start(
            python("import sys; sys.exit(137)"), tmp_path, memory_events=tmp_path / "absent"
        )
        return await child.wait()

    assert run(body()) == Exit(137, None, False)
