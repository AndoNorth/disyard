# dsy-exampleflow

The worked example of a workflow module: a package that adds a workflow to the workflow service without an edit of any core package.

## What it adds

`EchoWorkflow.run(text)` calls the activity `echo` on the task queue `dsy-exampleflow` and returns `{"text": text, "length": len(text)}`.
The entry point `exampleflow = "dsy_exampleflow:workers"` in the group `dsy.workflow.modules` resolves to `workers(ctx)`, which returns one `Worker` on the host's client.
The worker is built with `graceful_shutdown_timeout=ctx.drain`, so the service drains it with the built-in workers at SIGTERM.

## Rules of a module

A module imports only the names of `dsy_workflow.modules.__all__` and the names this README lists.
It owns its task queue name, and a thread pool for synchronous activities if it needs one.
It adds no database table, no event type and no metric, because each of those touches a core contract:
the event catalogue (`contracts/events`), the roles file (`R__roles.sql`) and the instrumentation catalogue (`contracts/instrumentation/metrics.yaml`).
A module that needs one opens a contract change on purpose.
A module that fails to import or to build, or that repeats a module name, is logged at error level and skipped, and a module worker that dies later does not end the host.

## More examples at no core cost

Kind A is one job document in `packages/jobexamples`.
Kind B is one workload case in `dsy_workloads`, run through the existing smoke job path.
