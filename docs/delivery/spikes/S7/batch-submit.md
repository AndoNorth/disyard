# Spike: one call of 100,000 jobs at T1 (S7)

Run on 2026-11-18.
Owner: the [dispatch and control API epic](https://github.com/AndoNorth/disyard/issues/24), lane S, filed as [spike issue 458](https://github.com/AndoNorth/disyard/issues/458).
Spec: [S7 in docs/delivery/epics/13-dispatch-api.md](../../epics/13-dispatch-api.md#s7-one-call-of-100000-jobs-at-t1), whose numbers feed D10 and #16 and gate nothing.
Method: [docs/delivery/method.md](../../method.md).
Throwaway code: [code/](code/), run as `nix develop -c docs/delivery/spikes/S7/code/run.sh --jobs 100000 --chunks 500,1000,2000 --repeat 2 --resend --objects-run 1000`.
Every number below is a `figure <name> <value>` line of that run, named `run<n>_c<chunk>_o<objects>.<figure>`.

## Result

**Pass.**
Chunks of 500 and of 1,000 each finish 100,000 rows, events and links with every chunk transaction under 5 s and a call peak of at most 24 MiB above the process baseline, in both runs.
Chunks of 2,000 fail the memory part of the pass line in both runs, at 31.3 MiB in both, while their transactions stay under 2.4 s.

| Chunk | Runs | Call time | Rows, events and links per second | Longest chunk transaction | Peak above baseline | Pass |
| --- | --- | --- | --- | --- | --- | --- |
| 500 | 1, 4 | 185.3 to 186.8 s | 535 to 540 | 0.745 to 0.818 s | 14.6 to 16.1 MiB | 2 of 2 |
| 1,000 | 2, 5 | 171.4 to 178.1 s | 562 to 583 | 1.171 to 1.247 s | 20.9 to 21.2 MiB | 2 of 2 |
| 2,000 | 3, 6 | 171.9 to 179.4 s | 557 to 582 | 2.124 to 2.309 s | 31.3 MiB | 0 of 2 |

**D10 keeps 1,000 entries per chunk.**
It runs as fast as 2,000 and 4 to 8 percent faster than 500, its longest transaction is a quarter of the 5 s line, and four calls at its peak add at most 85 MiB, inside the 96 MiB that D10 allows for them.
Its memory margin is 2.8 MiB per call, which finding 3 shows how to widen.
The limit of 100,000 entries per call stands: a call of that size takes about three minutes on this host.

## Question

How long does the submit sequence of D8 take for 100,000 jobs on this host, with `dsy_jobstate.insert_accepted`, `dsy_eventstore.insert_events`, `index_reference` once and `link_use` per job, in chunks of 500, 1,000 and 2,000, against the `dsy_itest` Postgres?

## Environment

| Item | Value |
| --- | --- |
| Host | WSL2, 20,002 MiB `MemTotal` (`host.mem_total_mib`), 16,284 MiB available at the start (`host.mem_available_mib`), 8 CPUs of an AMD Ryzen 5 7500F (`host.cpus`, `host.cpu_model`), kernel 6.6.87.2 (`host.kernel`) |
| Other work on the host | one T1 container of another worker held the other `dsy-t1` lock file for part of the run, and was not touched |
| Postgres | the `timescale/timescaledb` pin of `deploy/versions.yaml`, Postgres 16.15 (`postgres.version`), started by `dsy_itest.Session(containers=1)` at its 1 GiB memory cap, with the image's defaults: `shared_buffers` 256 MB, `synchronous_commit` on (`postgres.*`) |
| Client | Python 3.13.15 of the dev shell (`host.python`), `psycopg` as locked in `uv.lock`, the workspace packages at `main` |

## Method

- [code/measure.py](code/measure.py) starts one Postgres through `dsy_itest`, and for each run creates a fresh database, applies every migration with `dsy_migrate.apply`, and runs [code/submit.py](code/submit.py) in a process of its own, so each call's memory peak is its own.
- The call logs in twice, as the service of D13 does: once as a member of `dsy_dispatch` and once as a member of `dsy_artifacts`, so every write runs under the grants of `R__roles.sql`.
- The template is the three-task template of the plan tests with one integer parameter, compiled once with `dsy_plan.compile_plan` under the `ci` limits profile; its manifest reference is the one object indexed, and in run 7 the source bundle the template names is a second.
- The entries are generated one line at a time as bytes and parsed with `json.loads`, so the call holds no more of the body than a chunk, as a stream reader would.
- Per chunk, in the order of D8:
  - prepare: parse each line, check its index against the indexes seen so far in the call, `check_job_params`, derive the key `<batch key>/<index>` and the job id with `dsy_keys`, the fingerprint of D9, a fresh `traceparent`, and a `JobRow`;
  - links transaction, on the artifacts login: `link_use` for each job and each object, then commit (`index_reference` ran once per object before the first chunk);
  - rows transaction, on the dispatch login: `SELECT now()`, `insert_accepted` of the chunk, `build_event("job.accepted", ...)` for each inserted row with `time` equal to that `now()`, `insert_events`, commit;
  - answer: one JSON line per entry written to `/dev/null`.
- A chunk transaction is the longer of the two transactions of the chunk, and the pass line takes the longest over the call.
- Memory is the process's own: a warm-up call of 20 jobs loads every import and cache, then `VmRSS` after a garbage collection is the baseline, `VmHWM` is reset through `/proc/self/clear_refs` (`peak_reset` True in every run), and the call's peak is `VmHWM` at its end less the baseline.
- After each call the database is counted as the superuser: rows, `dev.dsy.job.accepted.v1` events and `artifact_use` rows of the call's jobs.
- With `--resend` the same call is sent again on the same connections, and every entry must come back `duplicate` with nothing new stored.
- The Python cost of 1,000 rows and events is timed apart from the database at the end of each process: `JobRow` to insert parameters, `build_event`, the event row conversion of `insert_events` (`dsy_events.to_event_row`), and `check_job_params`.
- Publishing is not measured: the spike has no broker, and the publish follows the answer lines outside both transactions (D8).

## Measured numbers

### Per run

| Figure | Run 1, 500 | Run 4, 500 | Run 2, 1,000 | Run 5, 1,000 | Run 3, 2,000 | Run 6, 2,000 | Names |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Call time | 186.8 s | 185.3 s | 178.1 s | 171.4 s | 179.4 s | 171.9 s | `.seconds` |
| Rows, events, links per second of the call | 535 | 540 | 562 | 583 | 557 | 582 | `.rows_per_s`, `.events_per_s`, `.links_per_s` |
| Rows per second inside the rows transactions | 910 | 946 | 984 | 1,036 | 974 | 1,036 | `.rows_per_rows_tx_s` |
| Links per second inside the links transactions | 1,478 | 1,408 | 1,467 | 1,484 | 1,471 | 1,476 | `.links_per_links_tx_s` |
| Longest rows transaction | 0.745 s | 0.818 s | 1.247 s | 1.171 s | 2.309 s | 2.124 s | `.longest_rows_tx_s` |
| Median, 99th percentile rows transaction | 0.533, 0.728 s | 0.516, 0.691 s | 1.009, 1.198 s | 0.960, 1.101 s | 2.038, 2.267 s | 1.911, 2.060 s | `.median_rows_tx_s`, `.p99_rows_tx_s` |
| Longest links transaction | 0.438 s | 0.467 s | 0.729 s | 0.706 s | 1.417 s | 1.399 s | `.longest_links_tx_s` |
| Baseline resident | 57.0 MiB | 57.1 MiB | 57.1 MiB | 57.3 MiB | 57.3 MiB | 57.1 MiB | `.baseline_mib` |
| Peak above baseline | 16.1 MiB | 14.6 MiB | 20.9 MiB | 21.2 MiB | **31.3 MiB** | **31.3 MiB** | `.peak_above_baseline_mib` |
| Stored rows, events, links | 100,000 each | 100,000 each | 100,000 each | 100,000 each | 100,000 each | 100,000 each | `.stored_*` |
| Database size after the call | 215.0 MiB | 215.1 MiB | 214.1 MiB | 214.3 MiB | 214.7 MiB | 214.7 MiB | `.db_mib` |
| Resend: time, duplicates, new rows and events | 152.7 s, 100,000, 0 | 151.7 s, 100,000, 0 | 167.2 s, 100,000, 0 | 146.0 s, 100,000, 0 | 147.1 s, 100,000, 0 | 147.7 s, 100,000, 0 | `.resend_*` |
| Pass | yes | yes | yes | yes | no | no | `.passes` |

### Where the call's time goes

Summed over the chunks of the call, in seconds (`.prepare_s`, `.insert_accepted_s`, `.build_events_s`, `.insert_events_s`, `.commit_s`, `.links_s`, `.answer_s`).

| Step | Run 1, 500 | Run 2, 1,000 | Run 5, 1,000 | Run 3, 2,000 |
| --- | --- | --- | --- | --- |
| Prepare (parse, params check, keys, row models) | 8.8 | 7.8 | 7.1 | 8.3 |
| `link_use`, links transaction | 67.6 | 68.2 | 67.4 | 68.0 |
| `insert_accepted` | 48.9 | 45.1 | 44.5 | 45.0 |
| `build_event` | 1.3 | 1.3 | 1.2 | 1.3 |
| `insert_events` | 58.9 | 54.9 | 50.5 | 56.1 |
| Commit of the rows transaction | 0.7 | 0.3 | 0.3 | 0.2 |
| Answer lines | 0.3 | 0.3 | 0.3 | 0.3 |

The two transactions take 95 to 96 percent of the call at every size (`.db_share`).

Python cost of 1,000 rows and events, timed apart from the database (`.python_*_ms_per_1000`), the same at every size:

| Step | Time per 1,000 |
| --- | --- |
| Event row conversion inside `insert_events` (`to_event_row`, which runs `to_bytes` and its schema check) | 436 to 510 ms |
| `check_job_params` | 44 to 59 ms |
| `build_event` | 10 to 13 ms |
| `JobRow` to insert parameters | 3 to 5 ms |

### Two objects per job

Run 7 links the manifest and the source bundle to each job, at chunks of 1,000.

| Figure | Run 7 | Run 2 and 5, one object | Names |
| --- | --- | --- | --- |
| Call time | 243.8 s | 171.4 to 178.1 s | `.seconds` |
| Jobs per second, links per second | 410, 820 | 562 to 583, the same | `.rows_per_s`, `.links_per_s` |
| `link_use` in total | 136.3 s | 67.4 to 68.2 s | `.links_s` |
| Longest links, rows transaction | 1.419 s, 1.250 s | 0.706 to 0.729 s, 1.171 to 1.247 s | `.longest_links_tx_s`, `.longest_rows_tx_s` |
| Peak above baseline | 22.0 MiB | 20.9 to 21.2 MiB | `.peak_above_baseline_mib` |
| Stored rows, events, links | 100,000, 100,000, 200,000 | 100,000 each | `.stored_*` |
| Resend: time, duplicates, new rows and events | 213.9 s, 100,000, 0 | 146.0 to 167.2 s | `.resend_*` |
| Pass | yes | yes | `.passes` |

A template that names a source bundle, the usual case, doubles the links and takes the call to about four minutes, and its longest transaction is then the links one, still under 1.5 s.

### How the peak grows

`VmHWM` above the baseline after the first chunk, at a tenth, at half and at the end of the call (`chunks[].hwm_kib` of `run<n>.json`):

| Chunk | After chunk 1 | 10 percent | 50 percent | End |
| --- | --- | --- | --- | --- |
| 500 (run 1) | 4.5 MiB | 7.7 MiB | 11.1 MiB | 16.1 MiB |
| 1,000 (run 2) | 9.0 MiB | 12.9 MiB | 15.8 MiB | 20.9 MiB |
| 2,000 (run 3) | 18.2 MiB | 23.4 MiB | 26.0 MiB | 31.3 MiB |

## Findings

1. **Chunk size sets the transaction length and the memory, not the rate.**
   The call runs at 535 to 583 jobs per second at every size, because every library call the sequence makes costs per job.
   The rows transaction costs about 1.0 to 1.2 ms per entry, so 5 s is reached near 4,000 entries per chunk, and the longest links transaction about 0.7 ms per entry.
2. **The call takes about three minutes for 100,000 jobs, a third more than the estimate of Q5.**
   Q5 estimated at least 2.5 minutes from the state epic's 979 rows and 1,980 events per second, before links; the links add 38 percent of the call.
   B.2's `test_submit_100000` takes about 3 minutes for the call and 2.5 more for its resend on this host before the HTTP route adds its own cost, and the 10,000-job test about 35 s for both by the same rate (an estimate).
3. **The peak is the chunk plus a part that grows with the call.**
   The chunk costs about 9 KiB per entry (4.5, 9.0 and 18.2 MiB after the first chunk), and the call adds about 12 MiB over 100,000 entries whatever the chunk size, most of it the set of indexes seen, which `DUPLICATE_INDEX` needs: a Python set of 100,000 integers holds about 7 MiB.
   B.2 can hold the seen indexes as a bit array of 100,000 bits (12.5 KiB), which leaves the 1,000 chunk near 14 MiB and widens its margin to about 10 MiB.
4. **The event schema check is the largest client cost inside the rows transaction.**
   `insert_events` converts each event through `to_bytes`, which runs the CloudEvents and profile JSON Schemas, at about 0.5 ms per event: about 48 of its 51 to 59 s per call, and close to half of each rows transaction.
   The conversion cannot leave the transaction, because the event's time is the transaction's `now()`, which is the rows' `created_at` (D8).
   The events are built by `build_event` from rows the API already checked, so a cheaper check for such events is the lever, which is #4's to weigh and #16's to measure.
5. **`insert_accepted` and `link_use` cost a round trip per job.**
   `insert_accepted` runs one `INSERT` per row (about 0.45 ms each) and `link_use` a `SELECT ... FOR NO KEY UPDATE` and an `INSERT` per job and object (about 0.68 ms each).
   A set-based form of each, one statement per chunk as `insert_events` already does with `unnest`, is the lever for the rate, and is a `revisit` of #3 and #12 for #16 to weigh, not a change this spike asks for.
6. **A resend costs about what the call costs.**
   Every entry came back `duplicate` and nothing new was stored, in 146 to 167 s: `insert_accepted` reads the existing row for each conflict, and `link_use` renews each link.
7. **The links lock the object row, one call at a time.**
   `link_use` takes `FOR NO KEY UPDATE` on the artifact row for each job, so two calls that name the same manifest or source bundle serialize their links transactions on that row, which this single-call spike does not exercise and #16 measures with four calls in flight.

## Caveats and what was not measured

- One host, two runs of each chunk size and one run of the two-object case, all seven passing their counts and their resends.
- The Postgres is the T1 container with its image defaults and a 1 GiB cap, on the same host as the client, so a round trip costs less than on the cluster, where the client is in another pod.
- No broker: the publish after the answer lines, the receipt and `job.published` (B.3) are outside these numbers and outside both transactions.
- One call at a time: D10 allows 4 calls per process and 2 per tenant, and the memory figure for four is four times one call's peak, not a measurement of four in flight.
- The template is validated and compiled once and not resolved through a `SourceResolver`, and the manifest is not stored in the object store, both of which happen once per call before the first chunk (D12).
- After the run, `ruff` reformatted the code to the repository's lint rules, which changed only docstrings and the order of imports in its syntax tree.
- The service baseline of 57 MiB is this process's, with the libraries of the submit path loaded and no HTTP server, Temporal client or broker client; D.1 measures the service's own.
