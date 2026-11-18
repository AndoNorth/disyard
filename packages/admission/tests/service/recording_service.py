"""The admission service as `python -m wdf_admission.service` runs it, configured from the
environment, except that each start goes to a recording fake, which moves the started row on to
`succeeded` as the workflow would, on the superuser connection of WDF_TEST_SUPERUSER_DSN.

It prints the started jobs and the peak resident memory of the process once it has drained.
"""

import asyncio
import logging
import os
import re
from pathlib import Path

import psycopg
from wdf_admission.service import READY_ENV, Config, serve
from wdf_broker.tenancy import load
from wdf_jobstate import transition


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = Config.from_env(os.environ)
    db = psycopg.connect(os.environ["WDF_TEST_SUPERUSER_DSN"], autocommit=True)
    started = []

    async def recording(row, accepted):
        started.append(row.job_id)
        transition(db, row.job_id, "admitted", "running")
        transition(db, row.job_id, "running", "succeeded")
        return "started"

    ready = Path(os.environ[READY_ENV])
    asyncio.run(serve(config, load(config.profile), start=recording, ready_file=ready))
    peak_kib = int(re.search(r"VmHWM:\s+(\d+) kB", Path("/proc/self/status").read_text())[1])
    print(f"started: {len(started)}")
    print(f"peak_rss_mib: {peak_kib / 1024:.1f}")


if __name__ == "__main__":
    main()
