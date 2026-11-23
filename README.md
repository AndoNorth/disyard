# Weft

Durable batch simulation, woven at scale.

<p align="center">
  <a href="https://github.com/AndoNorth/weft/actions/workflows/ci.yml"><img alt="ci" src="https://github.com/AndoNorth/weft/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="https://github.com/AndoNorth/weft/actions/workflows/kind-smoke.yml"><img alt="kind-smoke" src="https://github.com/AndoNorth/weft/actions/workflows/kind-smoke.yml/badge.svg?branch=main"></a>
  <a href="https://github.com/AndoNorth/weft/actions/workflows/state-modules.yml"><img alt="state-modules" src="https://github.com/AndoNorth/weft/actions/workflows/state-modules.yml/badge.svg?branch=main"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/AndoNorth/weft"></a>
</p>

Weft is a proof of concept that runs large scale digital validation as durable batch jobs on Temporal: a job compiles a testbench, runs it across many seeds and configurations, and aggregates the results, scaling to millions of jobs and to jobs that run for weeks on a fixed-size local Kubernetes cluster.
Guest workloads run as WebAssembly components.

![System context](docs/architecture/01-system-context.drawio.svg)

## Worked example

This animation follows one job from submission to completion.

![One job, end to end](docs/animations/media/job-walkthrough.gif)

## Getting started

See [docs/dev-setup.md](docs/dev-setup.md) for the host and the dev shell, and [docs/runbooks](docs/runbooks/README.md) to bring the system up, prove it, triage a failure and tear it down.

## License

Licensed under the [Apache License 2.0](LICENSE).
