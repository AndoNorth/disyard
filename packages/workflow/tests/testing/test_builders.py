"""The shared builders and factories of dsy_workflow.testing that need no server: the build id, the
plan and the accepted event, and the dev server command."""

from contextlib import contextmanager

from dsy_workflow import testing
from dsy_workflow.contract import naming


def test_build_id_keeps_the_one_set(monkeypatch):
    monkeypatch.setenv(naming().workflow.build_id_env, "given")
    assert testing.build_id() == "given"


def test_build_id_sets_the_default_when_none_is_set(monkeypatch):
    monkeypatch.delenv(naming().workflow.build_id_env, raising=False)
    import os

    assert testing.build_id() == "test-build"
    assert os.environ[naming().workflow.build_id_env] == "test-build"


def test_plan_binds_each_input_to_the_output_of_its_dependency():
    built = testing.plan(testing.FAN)
    by_id = {task.id: task for task in built.tasks}
    assert set(by_id) == set(testing.FAN)
    assert [(i.task, i.output) for i in by_id["sim-a"].inputs] == [("build", "component")]
    assert [i.task for i in by_id["agg"].inputs] == ["sim-a", "sim-b", "sim-c", "sim-d"]


def test_plan_flags_and_params_override_the_defaults():
    built = testing.plan(
        testing.FAN,
        max_parallel=2,
        retries=3,
        checkpoint=frozenset({"sim-a"}),
        allow_partial=frozenset({"agg"}),
        simulate={"workload": "pi", "args": {"n": 1}, "seed": 1},
    )
    by_id = {task.id: task for task in built.tasks}
    assert built.max_parallel == 2
    assert by_id["sim-a"].checkpoint and not by_id["sim-b"].checkpoint
    assert by_id["agg"].allow_partial and by_id["sim-b"].retries == 3
    assert by_id["sim-b"].params.args == {"n": 1}


def test_accepted_events_are_new_jobs_and_a_given_hash_names_the_manifest():
    first, second = testing.accepted_event(), testing.accepted_event("ab" * 32)
    assert first.jobid != second.jobid
    assert second.data["manifest"]["uri"] == f"s3://dsy-sources/sha256/ab/{'ab' * 32}"


class Container:
    host_ports = {0: 4242}

    def wait_ready(self, index):
        pass


class Session:
    def __init__(self):
        self.commands = []

    @contextmanager
    def container(self, image_key, ports, command):
        self.commands.append((image_key, command))
        yield Container()


def started(**options):
    session = Session()
    with testing.dev_server(session, **options) as address:
        pass
    return address, session.commands[0][1]


def test_dev_server_starts_the_namespace_of_naming():
    address, command = started()
    assert address == "127.0.0.1:4242"
    assert command[command.index("--namespace") + 1] == naming().namespace
    assert "--db-filename" not in command


def test_dev_server_parameters_are_the_flags_of_the_sites():
    _, command = started(namespace="", db_filename="/tmp/temporal.db")
    assert "--namespace" not in command
    assert command[command.index("--db-filename") + 1] == "/tmp/temporal.db"
