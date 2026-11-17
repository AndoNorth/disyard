"""The fixtures that admission's test directories share: the repository root, and a dev server per
test module for the tests that start workflows."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="module")
def temporal(t1) -> str:
    """The address of a dev server with the namespace of naming.yaml, one per test module, which
    is gone before the next module runs."""
    from wdf_workflow.contract import naming

    with t1.container(
        "temporalio/temporal",
        ports={0: 7233},
        command=(
            "server",
            "start-dev",
            "--ip",
            "0.0.0.0",
            "--headless",
            "--namespace",
            naming().namespace,
        ),
    ) as container:
        container.wait_ready(0)
        yield f"127.0.0.1:{container.host_ports[0]}"
