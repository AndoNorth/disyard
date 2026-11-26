import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from dsy_servicekit.lifecycle import ReadyFile, ThreadStop, stopping

TOY = Path(__file__).parent / "toy"


def test_ready_file_lifecycle(tmp_path):
    path = tmp_path / "ready"
    path.touch()
    ready = ReadyFile(path)
    ready.arm()
    assert not path.exists()
    ready.set()
    assert path.exists()
    ready.clear()
    ready.clear()
    assert not path.exists()


def test_ready_file_without_path_is_a_no_op():
    ready = ReadyFile(None)
    ready.arm()
    ready.set()
    ready.clear()


def test_stopping_sets_event_on_signals_and_removes_handlers():
    async def main(signum):
        with stopping() as stop:
            os.kill(os.getpid(), signum)
            await asyncio.wait_for(stop.wait(), 2)
        assert signal.getsignal(signum) in (signal.SIG_DFL, signal.default_int_handler)

    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.run(main(signum))


def test_stopping_accepts_a_loop():
    async def main():
        with stopping(asyncio.get_running_loop()) as stop:
            assert not stop.is_set()

    asyncio.run(main())


def test_thread_stop():
    stop = ThreadStop()
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        stop.install()
        for signum in old:
            stop.event.clear()
            os.kill(os.getpid(), signum)
            assert stop.event.wait(2)
    finally:
        for signum, handler in old.items():
            signal.signal(signum, handler)


def _toy(env, **kw):
    return subprocess.Popen(
        [sys.executable, "-m", "toy"],
        cwd=TOY.parent,
        env={**os.environ, "PYTHONPATH": str(TOY.parent), **env},
        stderr=subprocess.PIPE,
        text=True,
        **kw,
    )


def test_toy_without_env_exits_2():
    proc = _toy({"TOY_NAME": ""})
    assert proc.wait(10) == 2
    assert proc.stderr.read() == "toy: missing environment: TOY_NAME\n"


@pytest.mark.seam
def test_toy_ready_then_sigterm_exits_0(tmp_path):
    ready = tmp_path / "ready"
    proc = _toy({"TOY_NAME": "x", "TOY_READY_FILE": str(ready)})
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready.exists()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(10) == 0
    assert not ready.exists()
