"""Nothing of the package is stubbed any more, and every function of the library is exported."""

import wft_jobstate

FUNCTIONS = (
    "insert_accepted",
    "get_job",
    "transition",
    "request_action",
    "mark_published",
    "mark_delivered",
    "status_vocabulary",
)


def test_nothing_is_stubbed():
    assert wft_jobstate.STUBBED == ()


def test_the_package_exports_each_function():
    assert all(callable(getattr(wft_jobstate, name)) for name in FUNCTIONS)
