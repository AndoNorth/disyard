"""The publisher and the reconciler over dsy_broker's FakeBroker, behind the submit route of the
submit tests."""

import importlib.util
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from dsy_broker.client import Credentials
from dsy_dispatchapi.publish import Publisher
from dsy_dispatchapi.reconciler import Reconciler
from dsy_dispatchapi.resolver import StoreSourceResolver
from dsy_dispatchapi.submit import Submit

# The submit tests' fixtures, imported so that pytest registers them for this directory too.
_spec = importlib.util.spec_from_file_location(
    "submit_fixtures", Path(__file__).parents[1] / "submit" / "conftest.py"
)
_submit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_submit)
profile, store, source, template, entries = (
    _submit.profile,
    _submit.store,
    _submit.source,
    _submit.template,
    _submit.entries,
)
dispatch, artifacts, call = _submit.dispatch, _submit.artifacts, _submit.call

DISPATCH = Credentials("dispatch", "standin")


@pytest.fixture
def make_publisher(broker):
    def make(connect=None, timeout_s: float = 5.0) -> Publisher:
        return Publisher(connect or broker.connect, "fake", DISPATCH, timeout_s)

    return make


@pytest.fixture
def make_submit(store, profile, tenancy, tmp_path):
    def make(publisher: Publisher | None = None, tenancy=tenancy) -> Submit:
        resolver = StoreSourceResolver(store, tmp_path)
        return Submit(store, resolver, profile, tenancy, publisher=publisher)

    return make


@pytest.fixture
def reconcile(logins, make_publisher):
    """Runs one reconciler pass on a connection of its own."""

    def run(publisher: Publisher | None = None, grace: timedelta = timedelta(0)) -> None:
        reconciler = Reconciler(publisher or make_publisher(), grace)
        with psycopg.connect(logins["dispatch"], autocommit=True) as connection:
            reconciler.run_pass(connection)

    return run
