"""The dispatch and control API of one tenant, and the smoke job: a compile of the workload library
bundle, one simulate task over it and an aggregate over its result."""

import hashlib
import http.client
import json
import socket
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable
from urllib.parse import urlsplit

from dsy_workflow.harness import REAL_ENTRY

from . import Failed

CALL_TIMEOUT_S = 120.0


def request(
    url: str, method: str = "GET", body: bytes | None = None, timeout_s: float = CALL_TIMEOUT_S
) -> tuple[int, dict]:
    """The status and JSON body of one call."""
    call = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(call, timeout=timeout_s) as answer:
            return answer.status, json.loads(answer.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def stream(url: str, lines: Iterable[bytes], timeout_s: float = CALL_TIMEOUT_S) -> list[dict]:
    """The answer lines of an NDJSON call, read while a thread sends its body, as the contract asks
    of a client. A call whose connection the server drops, as a deleted pod does, returns the lines
    it answered, and a call refused before its answer raises Failed."""
    parts = urlsplit(url)
    sock = socket.create_connection((parts.hostname, parts.port), timeout=timeout_s)

    def send() -> None:
        try:
            sock.sendall(
                f"POST {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\n"
                "Content-Type: application/x-ndjson\r\nTransfer-Encoding: chunked\r\n\r\n".encode()
            )
            for line in lines:
                sock.sendall(b"%x\r\n%b\r\n" % (len(line), line))
            sock.sendall(b"0\r\n\r\n")
        except OSError:
            # The server ended the call and closed, so the rest of the body has nowhere to go.
            pass

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    answers = []
    try:
        response = http.client.HTTPResponse(sock)
        response.begin()
        if response.status != 200:
            raise Failed(f"{url} answered {response.status}: {response.read().decode()}")
        while text := response.readline():
            answers.append(json.loads(text))
    except (OSError, http.client.HTTPException):
        # The connection went with the pod.
        pass
    finally:
        sender.join(timeout_s)
        sock.close()
    return answers


def template(source: dict, simulate: dict, checkpoint: bool = False) -> dict:
    """A compile of the workload library bundle at `source`, one simulate task running `simulate`,
    and an aggregate over its result."""
    return {
        "schema": "urn:dsy:schema:job:1.0",
        "parameters": {},
        "tasks": {
            "build": {"type": "compile", "params": {"source": source, "entry": REAL_ENTRY}},
            "sim": {
                "type": "simulate",
                "inputs": [{"task": "build", "output": "component"}],
                "cpu": 0.25,
                "memory_mib": 384,
                "run_timeout_s": 600,
                "retries": 1,
                "checkpoint": checkpoint,
                "params": simulate,
            },
            "agg": {
                "type": "aggregate",
                "inputs": [{"task": "sim", "output": "result"}],
                "cpu": 1,
                "memory_mib": 512,
                "run_timeout_s": 600,
                "params": {"aggregation": "digest_list"},
            },
        },
    }


def batch(batch_key: str, document: dict, count: int) -> list[bytes]:
    """The lines of a batch submit body of `count` jobs of one template."""
    header = {"batch_key": batch_key, "template": document}
    entries = ({"index": n, "seed": n, "parameters": {}} for n in range(count))
    return [json.dumps(line).encode() + b"\n" for line in (header, *entries)]


def batch_key(prefix: str = "smoke") -> str:
    """A fresh batch key, so repeated runs on one cluster never dedup against each other."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Api:
    """The API of `tenant` at the `dispatch` and `control` listeners, each a base url."""

    def __init__(self, dispatch: str, control: str, tenant: str, timeout_s: float = CALL_TIMEOUT_S):
        self.dispatch, self.control, self.tenant = dispatch, control, tenant
        self.timeout_s = timeout_s

    def url(self, base: str, path: str) -> str:
        return f"{base}/v1/tenants/{self.tenant}/{path}"

    def upload_source(self, data: bytes) -> dict:
        """Uploads a source bundle, created or already present, and returns its reference."""
        url = self.url(self.dispatch, f"objects/source/{hashlib.sha256(data).hexdigest()}")
        code, answer = request(url, "PUT", data, self.timeout_s)
        if code not in (200, 201):
            raise Failed(f"the upload of the source bundle answered {code}: {answer}")
        return answer["reference"]

    def submit_one(self, document: dict, batch_key: str) -> str:
        """Submits one job of the template under `batch_key`, and returns its id."""
        lines = batch(batch_key, document, 1)
        answers = stream(self.url(self.dispatch, "batches"), lines, self.timeout_s)
        first = answers[0] if answers else {}
        if first.get("outcome") != "accepted":
            raise Failed(f"the job was not accepted: {first or 'no answer'}")
        return first["job_id"]

    def status(self, job_id: str) -> dict:
        code, answer = request(self.url(self.control, f"jobs/{job_id}"), timeout_s=self.timeout_s)
        if code != 200:
            raise Failed(f"the status read of {job_id} answered {code}: {answer}")
        return answer

    def cancel_batch(self, batch_key: str, reason: str = "smoke") -> dict | None:
        """Cancels every job of the batch, and returns the summary of the answer, or None when the
        answer ended before it."""
        url = self.url(self.control, f"batches/{batch_key}/cancel")
        answers = stream(url, [json.dumps({"reason": reason}).encode()], self.timeout_s)
        return answers[-1].get("summary") if answers else None
