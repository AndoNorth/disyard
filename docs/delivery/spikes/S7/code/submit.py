"""One submit call of the D8 sequence, without the broker, run in a process of its own so its memory
peak is its own. Prints one JSON object of figures on stdout.

    python submit.py --dispatch DSN --artifacts DSN --jobs 100000 --chunk 1000 [--objects 1]

The process connects both logins, runs a warm-up call of 20 jobs so every import and cache is
loaded, takes that as the service baseline, then runs the measured call over an entry stream that is
generated line by line and parsed as a server would read it.
"""

import argparse
import gc
import hashlib
import io
import json
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from dsy_artifactindex import index_reference, link_use
from dsy_events import build_event
from dsy_eventstore import insert_events
from dsy_eventstore.store import _columns
from dsy_jobdoc import parse_document
from dsy_jobdoc.profile_files import load_profile
from dsy_jobstate import JobRow, insert_accepted
from dsy_jobstate.jobs import _params
from dsy_keys import batch_id as derive_batch_id
from dsy_keys import job_id as derive_job_id
from dsy_plan import canonical_bytes, check_job_params, compile_plan, manifest_reference

ROOT = Path(__file__).resolve().parents[5]
TENANT = "acme"
SOURCE_SHA = "a" * 64
SOURCE = {
    "uri": f"s3://dsy-sources/sha256/aa/{SOURCE_SHA}",
    "sha256": SOURCE_SHA,
    "size": 2048,
    "name": "bundle.tar.gz",
    "kind": "source",
}
# The three-task template of the plan tests, with a seed and one integer parameter per job.
TEMPLATE = {
    "schema": "urn:dsy:schema:job:1.0",
    "parameters": {"seed": "seed", "n": "integer"},
    "tasks": {
        "build": {"type": "compile", "params": {"source": SOURCE, "entry": "sim"}},
        "sim": {
            "type": "simulate",
            "inputs": [{"task": "build", "output": "component"}],
            "cpu": 0.25,
            "memory_mib": 384,
            "run_timeout_s": 600,
            "retries": 1,
            "params": {"workload": "pi", "args": {"n": {"param": "n"}}, "seed": {"param": "seed"}},
        },
        "agg": {
            "type": "aggregate",
            "inputs": [{"task": "sim", "output": "result"}],
            "cpu": 1,
            "memory_mib": 512,
            "run_timeout_s": 600,
            "params": {"aggregation": "summary_stats", "args": {"blocks": ["pi"]}},
        },
    },
}


def proc_kib(field: str) -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(field + ":"):
            return int(line.split()[1])
    raise KeyError(field)


def reset_peak() -> bool:
    """Resets VmHWM to the current resident size, which Linux allows through clear_refs value 5."""
    try:
        Path("/proc/self/clear_refs").write_text("5")
        return True
    except OSError:
        return False


def fingerprint(job_class, manifest_sha256, parameters, priority_class, seed) -> str:
    value = {
        "job_class": job_class,
        "manifest_sha256": manifest_sha256,
        "parameters": parameters,
        "priority_class": priority_class,
        "seed": seed,
    }
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def traceparent() -> str:
    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


def entry_lines(jobs: int, salt: int):
    """The entry lines of a call as bytes, one at a time, as a stream reader hands them over."""
    for i in range(jobs):
        yield json.dumps(
            {"index": i, "seed": i + salt, "parameters": {"n": 1000 + i % 97}}
        ).encode()


def chunks(lines, size):
    chunk = []
    for line in lines:
        chunk.append(line)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


class Call:
    """The per-call state: the plan, the indexed objects and the indexes seen so far."""

    def __init__(self, profile, batch_key: str, objects: int):
        document = parse_document(json.dumps(TEMPLATE).encode(), profile.max_document_bytes)
        self.profile = profile
        self.plan = compile_plan(document, profile)
        self.manifest = manifest_reference(self.plan)
        assert len(canonical_bytes(self.plan)) == self.manifest["size"]
        self.uris = [self.manifest["uri"], SOURCE["uri"]][:objects]
        self.refs = [self.manifest, SOURCE][:objects]
        self.batch_key = batch_key
        self.batch_id = derive_batch_id(TENANT, batch_key)
        self.seen: set[int] = set()


def run_call(dispatch, artifacts, profile, jobs, chunk_size, objects, batch_key, sink, salt=0):
    call = Call(profile, batch_key, objects)
    figures = {"chunks": [], "rows": 0, "events": 0, "links": 0, "duplicates": 0}
    started = time.perf_counter()
    with artifacts.transaction():
        for ref in call.refs:
            index_reference(artifacts, ref)
    for raw in chunks(entry_lines(jobs, salt), chunk_size):
        t0 = time.perf_counter()
        rows = []
        for line in raw:
            entry = json.loads(line)
            if entry["index"] in call.seen:
                raise AssertionError("duplicate index in a generated stream")
            call.seen.add(entry["index"])
            params = {"seed": entry["seed"], "parameters": entry["parameters"]}
            issues = check_job_params(call.plan, params, profile)
            if issues:
                raise AssertionError(f"entry {entry['index']}: {issues}")
            key = f"{call.batch_key}/{entry['index']}"
            rows.append(
                JobRow(
                    job_id=derive_job_id(TENANT, key),
                    tenant=TENANT,
                    submission_key=key,
                    batch_id=call.batch_id,
                    priority_class="standard",
                    job_class="short",
                    manifest_ref=call.manifest,
                    schema_version="urn:dsy:schema:job.accepted:1.0",
                    traceparent=traceparent(),
                    seed=entry["seed"],
                    parameters=entry["parameters"],
                    fingerprint=fingerprint(
                        "short",
                        call.manifest["sha256"],
                        entry["parameters"],
                        "standard",
                        entry["seed"],
                    ),
                )
            )
        t1 = time.perf_counter()
        at = datetime.now(UTC)
        with artifacts.transaction():
            for row in rows:
                for uri in call.uris:
                    link_use(artifacts, row.job_id, uri, at)
        t2 = time.perf_counter()
        with dispatch.transaction():
            now = dispatch.execute("SELECT now()").fetchone()[0]
            result = insert_accepted(dispatch, rows)
            ta = time.perf_counter()
            inserted = set(result.inserted)
            events = [
                build_event(
                    "job.accepted",
                    source="/dsy/dispatch",
                    tenant=row.tenant,
                    jobid=row.job_id,
                    priorityclass=row.priority_class,
                    jobclass=row.job_class,
                    traceparent=row.traceparent,
                    tracestate=row.tracestate,
                    time=now,
                    data={
                        "manifest": row.manifest_ref,
                        "batch_id": row.batch_id,
                        "submission_key": row.submission_key,
                    },
                )
                for row in rows
                if row.job_id in inserted
            ]
            tb = time.perf_counter()
            stored = insert_events(dispatch, events)
            tc = time.perf_counter()
        t3 = time.perf_counter()
        for row in rows:
            outcome = "accepted" if row.job_id in inserted else "duplicate"
            answer = {"index": row.submission_key.rsplit("/", 1)[1], "outcome": outcome}
            answer |= {"job_id": row.job_id, "trace_id": row.traceparent[3:35]}
            sink.write(json.dumps(answer) + "\n")
        t4 = time.perf_counter()
        figures["rows"] += len(result.inserted)
        figures["duplicates"] += len(result.duplicates)
        figures["events"] += stored.inserted
        figures["links"] += len(rows) * len(call.uris)
        figures["chunks"].append(
            {
                "prepare": t1 - t0,
                "links_tx": t2 - t1,
                "rows_tx": t3 - t2,
                "insert_accepted": ta - t2,
                "build_events": tb - ta,
                "insert_events": tc - tb,
                "commit": t3 - tc,
                "answer": t4 - t3,
                "hwm_kib": proc_kib("VmHWM"),
            }
        )
    figures["seconds"] = time.perf_counter() - started
    return figures


def python_costs(dispatch, profile) -> dict:
    """The Python side of 1,000 rows and events, timed apart from the database, so the transaction
    times split into client work and round trips."""
    call = Call(profile, "cost", 1)
    now = dispatch.execute("SELECT now()").fetchone()[0]
    rows = [
        JobRow(
            job_id=derive_job_id(TENANT, f"cost/{i}"),
            tenant=TENANT,
            submission_key=f"cost/{i}",
            batch_id=call.batch_id,
            priority_class="standard",
            manifest_ref=call.manifest,
            schema_version="urn:dsy:schema:job.accepted:1.0",
            traceparent=traceparent(),
            seed=i,
            parameters={"n": i},
            fingerprint="b" * 64,
        )
        for i in range(1000)
    ]
    t0 = time.perf_counter()
    for row in rows:
        _params(row)
    t1 = time.perf_counter()
    events = [
        build_event(
            "job.accepted",
            source="/dsy/dispatch",
            tenant=row.tenant,
            jobid=row.job_id,
            priorityclass=row.priority_class,
            jobclass=row.job_class,
            traceparent=row.traceparent,
            time=now,
            data={
                "manifest": row.manifest_ref,
                "batch_id": row.batch_id,
                "submission_key": row.submission_key,
            },
        )
        for row in rows
    ]
    t2 = time.perf_counter()
    for event in events:
        _columns(event)
    t3 = time.perf_counter()
    for i in range(1000):
        check_job_params(call.plan, {"seed": i, "parameters": {"n": i}}, profile)
    t4 = time.perf_counter()
    return {
        "row_params": t1 - t0,
        "build_event": t2 - t1,
        "event_columns": t3 - t2,
        "check_job_params": t4 - t3,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dispatch", required=True)
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--jobs", type=int, default=100_000)
    parser.add_argument("--chunk", type=int, default=1000)
    parser.add_argument("--objects", type=int, choices=(1, 2), default=1)
    parser.add_argument("--batch-key", default="s7")
    parser.add_argument("--resend", action="store_true", help="send the same call a second time")
    args = parser.parse_args(argv)

    profile = load_profile("ci", ROOT, ROOT / "contracts/job/vectors/slots-fixture")
    with (
        psycopg.connect(args.dispatch, autocommit=True) as dispatch,
        psycopg.connect(args.artifacts, autocommit=True) as artifacts,
    ):
        sink = io.TextIOWrapper(open("/dev/null", "wb"))
        run_call(dispatch, artifacts, profile, 20, 20, args.objects, f"warm-{args.batch_key}", sink)
        gc.collect()
        baseline = proc_kib("VmRSS")
        peak_reset = reset_peak()
        figures = run_call(
            dispatch, artifacts, profile, args.jobs, args.chunk, args.objects, args.batch_key, sink
        )
        figures["peak_kib"] = proc_kib("VmHWM")
        if args.resend:
            gc.collect()
            figures["resend"] = run_call(
                dispatch,
                artifacts,
                profile,
                args.jobs,
                args.chunk,
                args.objects,
                args.batch_key,
                sink,
            )
            figures["resend"].pop("chunks")
        figures["python_per_1000"] = python_costs(dispatch, profile)
    figures |= {"baseline_kib": baseline, "peak_reset": peak_reset, "chunk": args.chunk}
    figures |= {"jobs": args.jobs, "objects": args.objects}
    json.dump(figures, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
