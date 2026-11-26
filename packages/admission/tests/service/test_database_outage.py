"""An unreachable database is reported as an outage inside the connect bound, and the loop goes on.

The database is a listening socket that never accepts: the kernel completes the TCP handshake and
the server never answers, which is what a black-holed Postgres looks like to libpq.
"""

import asyncio
import socket
import time
from collections import Counter
from contextlib import closing

from dsy_admission import service
from dsy_admission.intake import Intake
from dsy_admission.policy import Caps
from dsy_broker.tenancy import load
from dsy_broker.testing import FakeBroker

LIMIT_S = 3.0


def test_unreachable_database_opens_the_breaker_and_the_loop_continues():
    profile = load("ci")
    with closing(socket.socket()) as blackhole:
        blackhole.bind(("127.0.0.1", 0))
        blackhole.listen(1)
        dsn = f"postgresql://admission@127.0.0.1:{blackhole.getsockname()[1]}/dsy"
        jobs = service.Jobs(lambda: service.connect_database(dsn), None, "t-", Counter(), None)
        intake = Intake(
            profile,
            Caps.of(profile, None),
            connect_broker=FakeBroker("ci").connect,
            connect_db=lambda: service.connect_database(dsn),
            start=None,
            probe_temporal=None,
        )

        async def cycles() -> list[float]:
            took = []
            try:
                for _ in range(2):
                    began = time.monotonic()
                    await asyncio.wait_for(intake.cycle(jobs.work), LIMIT_S)
                    took.append(time.monotonic() - began)
                    # The breaker backs off before it probes again.
                    intake.breakers.of["database"].due = 0.0
            finally:
                # A connect still hanging ends here, so the loop can join its thread.
                blackhole.close()
            return took

        took = asyncio.run(cycles())

    assert all(seconds < LIMIT_S for seconds in took), took
    assert intake.breakers.of["database"].open
    assert intake.breakers.of["database"].backoff_s == 2.0
