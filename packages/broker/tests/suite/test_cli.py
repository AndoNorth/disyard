"""The suite without a broker: its vectors, the parse of the detailed endpoint and its usage."""

import pytest
from wdf_broker import suite

DETAILED = """\
# TYPE rabbitmq_detailed_queue_messages_ready gauge
rabbitmq_detailed_queue_messages_ready{vhost="/",queue="intake.acme.standard"} 3
rabbitmq_detailed_queue_messages_ready{vhost="/",queue="intake.dlq"} 0
rabbitmq_detailed_queue_messages_unacked{vhost="/",queue="intake.acme.standard"} 1
rabbitmq_detailed_queue_messages{vhost="/",queue="intake.acme.standard"} 4
rabbitmq_detailed_queue_consumers{vhost="/",queue="intake.acme.standard"} 2
"""


def test_parse_metrics_reads_ready_unacked_and_consumers():
    assert suite.parse_metrics(DETAILED) == {
        "intake.acme.standard": {"ready": 3, "unacked": 1, "consumers": 2},
        "intake.dlq": {"ready": 0, "unacked": 0, "consumers": 0},
    }


def test_vectors_and_intake_queue():
    assert suite.intake_queue() == "intake.acme.standard"
    assert suite.ACCEPTED in suite.golden()
    assert {"not-json", "wrong-major"} <= suite.poison().keys()
    assert all(set(expect) == {"error", "reason"} for _, expect in suite.poison().values())


def test_credentials_read_one_file_per_user(tmp_path):
    for user in ("dispatch", "admission", "operator"):
        (tmp_path / f"{user}.password").write_text(f"{user}-secret")
    users = suite.credentials(tmp_path)
    assert {u: (c.user, c.password) for u, c in users.items()} == {
        u: (u, f"{u}-secret") for u in ("dispatch", "admission", "operator")
    }


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["check"],
        ["--address", "a", "--metrics", "m", "--credentials-dir", "d"],
        ["--address", "a", "--metrics", "m", "--credentials-dir", "d", "fill"],
        ["--address", "a", "--metrics", "m", "--credentials-dir", "d", "nope"],
    ],
)
def test_usage(argv):
    assert suite.main(argv) == 2


def test_missing_credentials_is_usage(tmp_path, capsys):
    argv = ["--address", "a", "--metrics", "m", "--credentials-dir", str(tmp_path), "count"]
    assert suite.main(argv) == 2
    assert "dispatch.password" in capsys.readouterr().err


def test_unreachable_broker(tmp_path):
    for user in ("dispatch", "admission", "operator"):
        (tmp_path / f"{user}.password").write_text("x")
    argv = ["--credentials-dir", str(tmp_path), "--metrics", "http://127.0.0.1:1/metrics"]
    assert suite.main([*argv, "--address", "127.0.0.1:1", "fill", "--count", "1"]) == 3
    assert suite.main([*argv, "--address", "127.0.0.1:1", "count"]) == 3
