"""Level 4 negatives A: a failing compile as chain B with its partial variant (N1), and a simulate
that writes a malformed result block (N2). Scenario logic is in `dsy_acceptance.e2e.negatives`."""

from collections.abc import Callable, Iterator

import pytest
from dsy_acceptance.clients import api, postgres
from dsy_acceptance.e2e import negatives
from dsy_acceptance.smoke import forwarded
from dsy_clusterkit import INFRA


@pytest.fixture(scope="module")
def events() -> Iterator[Callable[[str], list]]:
    """The event rows of a job, read through a read-only connection held for the module."""
    with forwarded(INFRA, "pod/appdb-0", 5432) as address, postgres.connect(address) as conn:
        yield lambda job_id: postgres.events(conn, job_id)


@pytest.mark.scenario("N1")
def test_failing_compile(client: api.Api, events, batches):
    """Lower level proof of the variants: packages/workflow/tests/test_failures.py."""
    negatives.failing_compile(client, events, batches)


@pytest.mark.scenario("N2")
def test_malformed_block(client: api.Api, events, batches):
    """Lower level proof of the variants: packages/aggregate/tests/test_hardening.py."""
    negatives.malformed_block(client, events, batches)
