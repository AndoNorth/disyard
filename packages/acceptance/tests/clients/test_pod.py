"""The pod client against a canned kubectl, so no cluster is needed."""

from dsy_acceptance.clients import pod


def test_exec_in_runs_the_command_in_the_named_pod(monkeypatch):
    calls = []
    monkeypatch.setattr(pod, "kubectl", lambda *args: calls.append(args) or "out")
    assert pod.exec_in("kind-dsy", "dsy-workers", "w-0", "ls", "/", container="main") == "out"
    assert calls == [
        ("kind-dsy", "--namespace=dsy-workers", "exec", "w-0", "-c", "main", "--", "ls", "/")
    ]


def test_process_names_reads_proc_comm_without_ps(monkeypatch):
    calls = []
    monkeypatch.setattr(pod, "kubectl", lambda *args: calls.append(args) or "python\nsh\n\n")
    assert pod.process_names("kind-dsy", "dsy-workers", "w-0") == {"python", "sh"}
    assert calls[0][-3:] == ("sh", "-c", "cat /proc/[0-9]*/comm")
