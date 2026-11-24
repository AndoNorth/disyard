"""Runs the submit call of submit.py once per chunk size, each in a fresh database of one T1
Postgres started through dsy_itest, checks the counts it left, and prints one figure line, `figure
<name> <value>`, per number of docs/delivery/spikes/S7/batch-submit.md, then a summary table.

    python measure.py [--jobs 100000] [--chunks 500,1000,2000] [--repeat 1] [--resend]
                      [--objects-run 1000] [--out DIR]
"""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

from dsy_itest import Session
from dsy_migrate import apply, discover
from dsy_migrate.fixtures import IMAGE, PASSWORD, TELEMETRY_OFF, _connect
from psycopg import sql
from psycopg.conninfo import make_conninfo

HERE = Path(__file__).resolve().parent
LOGINS = {"dsy_dispatch": "s7_dispatch", "dsy_artifacts": "s7_artifacts"}
# The pass line of the spike block: no chunk transaction above 5 s, and a call peak above the
# process baseline of at most 24 MiB.
MAX_TX_S = 5.0
MAX_PEAK_MIB = 24.0


def figure(out: Path, name: str, value) -> None:
    line = f"figure {name} {value}"
    print(line, flush=True)
    with (out / "figures.txt").open("a") as f:
        f.write(line + "\n")


def say(text: str) -> None:
    print(f"[{time.strftime('%T')}] {text}", file=sys.stderr, flush=True)


def counts(admin) -> dict:
    one = lambda q: admin.execute(q).fetchone()[0]  # noqa: E731
    return {
        "rows": one("SELECT count(*) FROM accepted_job WHERE submission_key NOT LIKE 'warm-%'"),
        "events": one(
            "SELECT count(*) FROM event e JOIN accepted_job j USING (job_id)"
            " WHERE e.type = 'dev.dsy.job.accepted.v1' AND j.submission_key NOT LIKE 'warm-%'"
        ),
        "links": one(
            "SELECT count(*) FROM artifact_use u JOIN accepted_job j USING (job_id)"
            " WHERE j.submission_key NOT LIKE 'warm-%'"
        ),
        "db_mib": round(one("SELECT pg_database_size(current_database())") / 2**20, 1),
    }


def run_one(port: int, out: Path, name: str, jobs: int, chunk: int, objects: int, resend: bool):
    with _connect(port) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = f"postgresql://postgres:{PASSWORD}@127.0.0.1:{port}/{name}"
    apply(dsn, discover())
    # The group roles exist once the first database is migrated, and logins are server wide.
    with _connect(port) as admin:
        for group, login in LOGINS.items():
            if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (login,)).fetchone():
                admin.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                        sql.Identifier(login), sql.Literal(PASSWORD), sql.Identifier(group)
                    )
                )
    command = [sys.executable, str(HERE / "submit.py"), "--jobs", str(jobs), "--chunk", str(chunk)]
    command += ["--objects", str(objects)] + (["--resend"] if resend else [])
    command += ["--dispatch", make_conninfo(dsn, user=LOGINS["dsy_dispatch"], password=PASSWORD)]
    command += ["--artifacts", make_conninfo(dsn, user=LOGINS["dsy_artifacts"], password=PASSWORD)]
    say(f"{name}: {jobs} jobs in chunks of {chunk}, {objects} object(s) per job")
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        sys.stderr.write(done.stderr)
        raise SystemExit(f"{name}: submit.py exited {done.returncode}")
    result = json.loads(done.stdout)
    (out / f"{name}.json").write_text(json.dumps(result, indent=1))
    with _connect(port, name) as admin:
        result["stored"] = counts(admin)
    with _connect(port) as admin:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
    return result


def report(out: Path, name: str, r: dict) -> dict:
    tx = [max(c["links_tx"], c["rows_tx"]) for c in r["chunks"]]
    rows_tx = [c["rows_tx"] for c in r["chunks"]]
    links_tx = [c["links_tx"] for c in r["chunks"]]
    seconds = r["seconds"]
    peak = (r["peak_kib"] - r["baseline_kib"]) / 1024
    db_seconds = sum(rows_tx) + sum(links_tx)
    s = {
        "chunk": r["chunk"],
        "seconds": round(seconds, 1),
        "rows_per_s": round(r["rows"] / seconds),
        "events_per_s": round(r["events"] / seconds),
        "links_per_s": round(r["links"] / seconds),
        "rows_per_rows_tx_s": round(r["rows"] / sum(rows_tx)),
        "links_per_links_tx_s": round(r["links"] / sum(links_tx)),
        "longest_tx_s": round(max(tx), 3),
        "longest_rows_tx_s": round(max(rows_tx), 3),
        "longest_links_tx_s": round(max(links_tx), 3),
        "median_rows_tx_s": round(statistics.median(rows_tx), 3),
        "p99_rows_tx_s": round(sorted(rows_tx)[int(len(rows_tx) * 0.99) - 1], 3),
        "first_rows_tx_s": round(rows_tx[0], 3),
        "last_rows_tx_s": round(rows_tx[-1], 3),
        "db_share": round(db_seconds / seconds, 2),
        "prepare_s": round(sum(c["prepare"] for c in r["chunks"]), 1),
        "insert_accepted_s": round(sum(c["insert_accepted"] for c in r["chunks"]), 1),
        "build_events_s": round(sum(c["build_events"] for c in r["chunks"]), 1),
        "insert_events_s": round(sum(c["insert_events"] for c in r["chunks"]), 1),
        "commit_s": round(sum(c["commit"] for c in r["chunks"]), 1),
        "links_s": round(sum(links_tx), 1),
        "answer_s": round(sum(c["answer"] for c in r["chunks"]), 1),
        "baseline_mib": round(r["baseline_kib"] / 1024, 1),
        "peak_above_baseline_mib": round(peak, 1),
        "peak_reset": r["peak_reset"],
        "stored_rows": r["stored"]["rows"],
        "stored_events": r["stored"]["events"],
        "stored_links": r["stored"]["links"],
        "db_mib": r["stored"]["db_mib"],
        "passes": max(tx) <= MAX_TX_S
        and peak <= MAX_PEAK_MIB
        and r["stored"]["rows"] == r["jobs"]
        and r["stored"]["events"] == r["jobs"]
        and r["stored"]["links"] == r["jobs"] * r["objects"],
    }
    for key, value in r["python_per_1000"].items():
        s[f"python_{key}_ms_per_1000"] = round(value * 1000)
    if "resend" in r:
        again = r["resend"]
        s |= {
            "resend_seconds": round(again["seconds"], 1),
            "resend_duplicates": again["duplicates"],
            "resend_new_rows": again["rows"],
            "resend_new_events": again["events"],
        }
    for key, value in s.items():
        figure(out, f"{name}.{key}", value)
    return s


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=100_000)
    parser.add_argument("--chunks", default="500,1000,2000")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--resend", action="store_true")
    parser.add_argument("--objects-run", type=int, default=0, help="chunk size of a 2-object run")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    out = args.out or Path(os.environ.get("TMPDIR", "/tmp")) / f"dsy-s7.{int(time.time())}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "figures.txt").write_text("")
    say(f"out {out}")

    meminfo = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    figure(out, "host.cpus", os.cpu_count())
    figure(out, "host.mem_total_mib", int(meminfo["MemTotal"].split()[0]) // 1024)
    figure(out, "host.mem_available_mib", int(meminfo["MemAvailable"].split()[0]) // 1024)
    figure(out, "host.kernel", platform.release())
    figure(out, "host.python", platform.python_version())
    figure(out, "host.cpu_model", platform.processor() or _cpu_model())

    plan = [(c, 1) for c in map(int, args.chunks.split(","))] * args.repeat
    if args.objects_run:
        plan.append((args.objects_run, 2))
    summaries = []
    with (
        Session(containers=1) as session,
        session.container(
            IMAGE, ports={0: 5432}, command=TELEMETRY_OFF, env={"POSTGRES_PASSWORD": PASSWORD}
        ) as pg,
    ):
        port = pg.host_ports[0]
        with _connect(port) as admin:
            figure(out, "postgres.version", admin.execute("SHOW server_version").fetchone()[0])
            figure(
                out, "postgres.shared_buffers", admin.execute("SHOW shared_buffers").fetchone()[0]
            )
            figure(
                out,
                "postgres.synchronous_commit",
                admin.execute("SHOW synchronous_commit").fetchone()[0],
            )
        for n, (chunk, objects) in enumerate(plan, 1):
            name = f"run{n}_c{chunk}_o{objects}"
            result = run_one(port, out, name, args.jobs, chunk, objects, args.resend)
            summaries.append((name, report(out, name, result)))

    keys = [
        "chunk",
        "seconds",
        "rows_per_s",
        "events_per_s",
        "links_per_s",
        "longest_tx_s",
        "longest_rows_tx_s",
        "longest_links_tx_s",
        "peak_above_baseline_mib",
        "passes",
    ]
    print("\n" + " | ".join(["run", *keys]))
    for name, s in summaries:
        print(" | ".join([name, *(str(s[k]) for k in keys)]))
    return 0


def _cpu_model() -> str:
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip().replace(" ", "_")
    return "unknown"


if __name__ == "__main__":
    sys.exit(main())
