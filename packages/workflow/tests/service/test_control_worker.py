"""control_worker polls the build's control queue with the given executor and passes options on."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from dsy_workflow import worker
from dsy_workflow.service import control_queue


def test_control_worker_options(monkeypatch):
    built = {}
    monkeypatch.setattr(worker, "Worker", lambda client, **kwargs: built.update(kwargs, c=client))
    activities = [lambda: None]
    with ThreadPoolExecutor(1) as pool:
        worker.control_worker(
            "client", "b1", activities, pool, graceful_shutdown_timeout=timedelta(seconds=3)
        )
        assert built == {
            "c": "client",
            "task_queue": control_queue("b1"),
            "activities": activities,
            "activity_executor": pool,
            "max_concurrent_activities": worker.THREADS,
            "graceful_shutdown_timeout": timedelta(seconds=3),
        }
