"""The service in a process of its own, as the pod runs it, over Postgres and the Temporal dev
server, with the store in a directory and FakeBroker: its memory under the most calls it takes at
once, and its stop in the middle of a call."""

import hashlib
import http.client
import itertools
import os
import signal
import threading

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.pair]

# The resident peak the process must stay under, inside the 256 MiB limit of its pod.
PEAK_MIB = 230
CALLS = 4
JOBS_PER_CALL = 3_000
INDEX_BYTES = 128 * 1024 * 1024
ACCEPTED = "SELECT count(*) FROM event WHERE type = 'dev.wdf.job.accepted.v1'"


def peak_mib(pid: int) -> float:
    for line in open(f"/proc/{pid}/status"):
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024
    raise AssertionError("no VmHWM")


def index_object(path, seed: int) -> str:
    """Writes an index object of INDEX_BYTES, the cap of its kind, and returns its hash."""
    digest, block = hashlib.sha256(), hashlib.sha256(str(seed).encode()).digest() * 32_768
    with open(path, "wb") as out:
        for _ in range(INDEX_BYTES // len(block)):
            out.write(block)
            digest.update(block)
    return digest.hexdigest()


def put_file(url: str, path, sha: str) -> int:
    host, port = url.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=120)
    try:
        with open(path, "rb") as body:
            connection.request(
                "PUT",
                f"/v1/tenants/acme/objects/index/{sha}",
                body=body,
                headers={"Content-Length": str(os.path.getsize(path))},
            )
        return connection.getresponse().status
    finally:
        connection.close()


def test_memory_with_four_calls_and_two_uploads(spawn, logins, temporal, tmp_path, kit):
    env = kit.environment(logins, temporal, calls_per_tenant=CALLS)
    with spawn(env, tmp_path / "store", tmp_path / "service.log") as service:
        source = kit.upload_bundle(service.dispatch, tmp_path)
        objects = [tmp_path / f"index-{n}" for n in range(2)]
        hashes = [index_object(path, n) for n, path in enumerate(objects)]
        tallies, statuses = {}, {}

        def submit(n: int) -> None:
            call = kit.Call(service.dispatch, kit.batch(source, f"memory-{n}", JOBS_PER_CALL))
            outcomes = [answer.get("outcome") for answer in call.answers()]
            tallies[n] = outcomes.count("accepted")

        def upload(n: int) -> None:
            statuses[n] = put_file(service.dispatch, objects[n], hashes[n])

        threads = [threading.Thread(target=submit, args=(n,)) for n in range(CALLS)]
        threads += [threading.Thread(target=upload, args=(n,)) for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        peak = peak_mib(service.proc.pid)

    print(f"resident peak: {peak:.1f} MiB")
    assert tallies == dict.fromkeys(range(CALLS), JOBS_PER_CALL)
    assert statuses == {0: 201, 1: 201}
    assert peak < PEAK_MIB


def test_sigterm_mid_call_then_resend(spawn, logins, temporal, tmp_path, db, kit):
    total, first_chunk = 1_500, 1_000
    log = tmp_path / "service.log"
    env = kit.environment(logins, temporal)
    with spawn(env, tmp_path / "store", log) as service:
        source = kit.upload_bundle(service.dispatch, tmp_path)
        lines = list(kit.batch(source, "sigterm", total))
        call = kit.Call(service.dispatch, lines, pause_after=1 + first_chunk)
        answers = call.answers()
        first = list(itertools.islice(answers, first_chunk))
        # The call still publishes the first chunk, or waits for the next entry, when the stop
        # lands, and either way it takes at most the one entry it is reading.
        service.proc.send_signal(signal.SIGTERM)
        threading.Event().wait(1)
        call.go.set()
        rest = list(answers)
        assert service.proc.wait(60) == 0, log.read_text()

    answered = first + rest[:-1]
    assert {answer["outcome"] for answer in answered} == {"accepted"}
    assert first_chunk <= len(answered) <= first_chunk + 1
    assert rest[-1]["error"]["code"] == "TOO_MANY_CALLS"
    counts = db.execute(f"SELECT (SELECT count(*) FROM accepted_job), ({ACCEPTED})").fetchone()
    assert counts == (len(answered), len(answered))

    with spawn(kit.environment(logins, temporal), tmp_path / "store", log) as service:
        resent = list(kit.Call(service.dispatch, lines).answers())
    outcomes = [answer["outcome"] for answer in resent[:-1]]
    assert outcomes == ["duplicate"] * len(answered) + ["accepted"] * (total - len(answered))
    assert resent[-1]["summary"]["accepted"] == total - len(answered)
    counts = db.execute(f"SELECT (SELECT count(*) FROM accepted_job), ({ACCEPTED})").fetchone()
    assert counts == (total, total)
