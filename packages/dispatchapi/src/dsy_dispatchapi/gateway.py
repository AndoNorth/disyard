"""The Temporal gateway: the workflow port over one `temporalio` client.

The client is asynchronous and the handlers and loops that call the port are threads, so the client
lives on an asyncio loop of its own thread and every call is handed to it and waited on with a
timeout. The client connects lazily and reconnects by itself, so the gateway is built while Temporal
is down and answers again once it is back.
"""

import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any

from dsy_keys import workflow_id
from dsy_temporal.client import connect
from dsy_workflow.contract import naming
from dsy_workflow.models import ControlSignal, StatusQuery
from temporalio.client import Client, WorkflowQueryFailedError
from temporalio.runtime import Runtime
from temporalio.service import RPCError, RPCStatusCode

from .ports import SIGNALS, NoWorkflow, SignalName, TemporalUnavailable

# How long a describe or a signal may take before Temporal counts as unavailable (an estimate).
CALL_TIMEOUT_S = 5.0
# How long the status query may take, so a status read never waits on a busy worker (an estimate).
QUERY_TIMEOUT_S = 2.0
# What the waiting thread adds to an RPC timeout, so the RPC's own deadline answers first.
GRACE_S = 1.0


class TemporalGateway:
    """The WorkflowGateway of the service, over one client of the namespace of naming.yaml."""

    def __init__(
        self,
        address: str,
        call_timeout_s: float = CALL_TIMEOUT_S,
        query_timeout_s: float = QUERY_TIMEOUT_S,
        runtime: Runtime | None = None,
    ):
        self.call_timeout = timedelta(seconds=call_timeout_s)
        self.query_timeout = timedelta(seconds=query_timeout_s)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="temporal-gateway", daemon=True
        )
        self._thread.start()
        self._client: Client = self._run(
            connect(
                address, naming().namespace, lazy=True, **({"runtime": runtime} if runtime else {})
            ),
            self.call_timeout,
        )

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()

    def __enter__(self) -> "TemporalGateway":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def exists(self, job_id: str) -> bool:
        try:
            self._run(self._handle(job_id).describe(rpc_timeout=self.call_timeout))
        except NoWorkflow:
            return False
        return True

    def signal(self, job_id: str, name: SignalName, signal: ControlSignal) -> None:
        if name not in SIGNALS:
            raise ValueError(f"the API never sends the signal {name!r}")
        self._run(self._handle(job_id).signal(name, signal, rpc_timeout=self.call_timeout))

    def status(self, job_id: str) -> StatusQuery:
        return self._run(
            self._handle(job_id).query(
                naming().queries[0], result_type=StatusQuery, rpc_timeout=self.query_timeout
            ),
            self.query_timeout,
        )

    def _handle(self, job_id: str):
        return self._client.get_workflow_handle(workflow_id(job_id))

    def _run(self, call: Coroutine[Any, Any, Any], timeout: timedelta | None = None) -> Any:
        """Runs `call` on the client's loop and waits for it, raising NoWorkflow for a workflow
        that Temporal does not hold, or holds closed when it is signalled, and TemporalUnavailable
        for any other failure to answer."""
        future = asyncio.run_coroutine_threadsafe(call, self._loop)
        wait_s = (timeout or self.call_timeout).total_seconds() + GRACE_S
        try:
            return future.result(wait_s)
        except concurrent.futures.TimeoutError as error:
            future.cancel()
            raise TemporalUnavailable(f"no answer in {wait_s:.0f} s") from error
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                raise NoWorkflow(str(error)) from error
            raise TemporalUnavailable(f"{error.status.name}: {error}") from error
        except (WorkflowQueryFailedError, RuntimeError) as error:
            # A refused query, or a client that could not connect, tells nothing about the job.
            raise TemporalUnavailable(str(error)) from error
