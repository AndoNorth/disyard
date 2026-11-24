# Validation matrix

Every check the repository has, what it proves, what it costs and when to run it.
Run them in the order of the table: each row is more expensive than the one above it, and fails for a reason the rows above cannot see.

Time is one of three kinds, and a cell says which.
A measured time is from one run on the author's WSL2 host with warm caches, so it is a scale and not a promise.
A budget is a limit the code or the workflow enforces.
`not measured` means no run was timed, and the cell names what it depends on.

## The checks

| Check | Command | Proves | Time | Needs | Run it |
| --- | --- | --- | --- | --- | --- |
| Lint | `nix develop -c just lint` | ruff, yamllint, shellcheck, actionlint, kubeconform, nixfmt and the old-name check pass, and each linter rejects its own bad fixture | about 10 s, measured | the flake only | before every commit, the hooks run most of it |
| Unit tests | `nix develop -c just test`, or `nix develop -c scripts/test.sh <path>` for one package | every package's logic, with fakes for the broker, Temporal and the stack | tens of minutes for the whole tree, not measured to the end, `scripts/test.sh packages/obs` took about 30 s | the flake only | before every push |
| Alert rule tests | `nix develop -c uv run --frozen pytest packages/obs/tests/rules -q` | every alert rule passes `promtool check rules`, its `promtool test rules` cases pass, it has a severity and a runbook entry | about 15 s, measured | the flake only | after any change to `deploy/obs/prometheus/rules/` or [alert response](alerts.md) |
| Contracts map | `nix develop -c just contracts-map` | every directory under `contracts/` has a row in the [contracts map](../contracts.md) and no row points at a missing path | seconds, not measured | the flake only | after adding a contract directory |
| Link check | `scripts/check-links.sh` | no relative Markdown link is dead | seconds, not measured | the flake only | after any Markdown change, `just ci` runs it |
| Integration tests | `nix develop -c just itest [domain]` | the services against real containers started by `dsy_itest`: RabbitMQ, Postgres, the object store, Temporal | not measured, grows with the domains run | Docker, free ports (`just ports`) | before a push that touches a service, `just itest broker` limits it to one package |
| Pre-PR gate | `nix develop -c just pre-pr` | the docs path of `just ci`, the unit tests of the changed packages and their dependents, and the integration tests of the changed packages | not measured, depends on the diff | Docker for the integration part | before opening a pull request |
| Full CI gate | `nix develop -c just ci` | everything CI's `check` job runs: lint, links, contracts map, self-tests, every shard's unit and integration tests, and `nix flake check` | not measured, the longest local check | Docker | before a change outside `packages/`, where `just pre-pr` says `full run required` |
| Security scans | `nix develop -c just scan [deps \| images \| secrets]` | no high or critical finding in the locked dependencies, in each service image, or in the git history, beyond the accepted ones in `security/trivy-ignore.yaml` | not measured, `images` builds every image | `images` needs Docker, `deps` and `images` fetch the Trivy database | before a release, CI runs it on every pull request and weekly |
| Kind smoke (CI) | the `kind-smoke` workflow: `just up`, then `just down` | a six-node cluster comes up on a clean runner for preset `ci` and goes down again | 30 min, budget of the workflow | a GitHub runner | automatic, skipped for a documentation-only pull request |
| State modules (CI) | the `state-modules` workflow: `just up`, `scripts/tilt/run.sh ci`, then the migration check | the database, Temporal database and object store modules deploy and migrate | not measured | a GitHub runner | automatic, on a change to those paths |
| Node limit canaries | `nix develop -c scripts/cluster/canary.sh all` | the node limits are real: a memory hog is OOM-killed, an oversized pod stays Pending, the pool taint holds, a spinner is throttled | not measured | a running cluster | after changing a preset or a node size |
| Cluster verify | `nix develop -c just cluster-verify` | the running cluster is the one its preset describes and every module is ready, within budget, with nothing pressured and every port on loopback | tens of seconds, 90 s limit inside `just smoke` | a running cluster with the modules deployed | after `just up` and after any host event |
| Smoke | `nix develop -c just smoke` | every layer answers and one real job goes through and produces the known answer, see [the rows](#the-smoke-rows) | 300 s, budget | a running `ci` cluster with the modules deployed | after every deploy, and before any larger run |
| API scenarios | `uv run --frozen python -m dsy_dispatchapi.smoke --scenario <name>` | submit, cancel, pause and resume, and a pod restart in the middle of a batch, through the deployed API | not measured | a running `ci` cluster, free of other jobs for `restart` | when the dispatch or control path changed |
| Failure drills | the steps of [a drill](drills/README.md) | an alert, a dashboard and a log show the failure that was caused, and the recovery works | 5 to 20 minutes for each drill, measured | a running `ci` cluster with the observability stack on | after changing an alert rule, a dashboard or a runbook entry |
| Live check | [dashboards and wiring](drills/live-check.md) | every dashboard loads and each service's metrics, logs and traces reach the stack | about 15 minutes, not measured to the end | a running `ci` cluster with the observability stack on, after `just smoke` | after changing a dashboard or a service's telemetry |
| Acceptance gate tests | `nix develop -c uv run --frozen pytest packages/acceptance -q` | the smoke driver's own logic against fakes, with no cluster | seconds, not measured | the flake only | when `packages/acceptance` changed |

### Not delivered yet

These are named by recipes or presets and prove nothing today.

| Check | State |
| --- | --- |
| `just e2e` | level 4 of the [test ladder](../acceptance-ladder.md), a stub that exits 3 with `not implemented: owned by #17`, and the ladder lists its scenarios, budget and direct reads |
| `just load-test` | a stub that exits 3 |
| `just characterize` | a stub that exits 3 |
| Soak | the `soak` preset and `scripts/tilt/run.sh soak` exist, and no soak run or pass criterion does yet |

A stub exits 3 on purpose, so a script never mistakes it for success.
The levels these checks belong to, and the rules that keep each behaviour at one level, are in the [test ladder](../acceptance-ladder.md).

## The smoke rows

`just smoke` prints one `ok`, `FAIL` or `skip` line per row and a count line, `smoke ci: <n> ok, <n> failed, <n> skipped, <s> s`.
Rows 1 to 5 always all run, so a dead layer fails its own row and the rest still print.
Rows 6 to 9 run only when rows 1 to 5 are ok, and a `skip` names the row that failed first.

| Row | Proves | A failure points at |
| --- | --- | --- |
| `cluster-verify` | the cluster matches its preset and every module is ready | run `just cluster-verify` and read its `FAIL` lines |
| `api` | both API listeners answer `/readyz`, which covers the database logins, a store head and Temporal | [dispatch API readiness](../../deploy/dispatchapi/README.md#readiness) |
| `broker` | the broker answers and its dead letters are counted as the baseline | [DsyDeadLetterQueueNotEmpty](alerts.md#dsydeadletterqueuenotempty), `kubectl --context kind-dsy -n dsy-infra get pod rabbitmq-0` |
| `temporal` | the namespace answers and a deployed workflow build is current, made current when none was | [DsyTemporalWorkerUnhealthy](alerts.md#dsytemporalworkerunhealthy) |
| `placement` | placement answers with every pool of the naming contract | [placement](../../deploy/placement/README.md#looking-at-it) |
| `job-submit` | the workload library uploads and one job is accepted | the `dispatch` log |
| `job-run` | the job ends `succeeded`, and a sample taken while it ran showed its compile done | the [triage guide](triage.md#jobs-stuck) |
| `job-result` | the simulate result holds the known answer of the level 0 echo vector | the `simulate-worker` log |
| `job-aggregate` | the aggregate summary is stored under its digest and matches its reference | [DsyAggregateFailuresHigh](alerts.md#dsyaggregatefailureshigh) |
| `dead-letters` | the job left no dead letter beyond the baseline | [DsyAdmissionDeadLettered](alerts.md#dsyadmissiondeadlettered) |
| `telemetry` | with the observability switch on, the stack holds the run's logs, traces and profiles and every scrape target is up | [DsyTargetDown](alerts.md#dsytargetdown), and with the switch off the row prints `observability off, not checked` |

The exit code is 0 when every row is ok, 1 when a row failed or the 300 s budget ran out, 2 for usage (a preset other than `ci`, or a missing tool), and 4 when refused (no cluster `dsy`, or one for another preset, or the lock not taken in 900 s).
When the budget runs out after the job was submitted, the run cancels its batch so a late job does not hold the pools into the next run.
With the stack on, the run also pushes its verdicts to Prometheus, and the `smoke` dashboard shows them over runs.

## Which check for which change

| You changed | Run at least |
| --- | --- |
| Documentation only | `just lint`, `scripts/check-links.sh`, `just pre-pr` |
| One package under `packages/` | `just test`, `just pre-pr` |
| An alert rule or its runbook entry | the alert rule tests, `just lint` |
| A module under `deploy/` | `just ci`, then a cluster: `just up`, `scripts/tilt/run.sh ci`, `just smoke` |
| A preset or a node size | `just ci`, `just nuke`, `just up`, `just cluster-verify`, `scripts/cluster/canary.sh all` |
| A contract | `just contracts-map`, `just ci` |
| Dependencies or an image | `just scan` |
