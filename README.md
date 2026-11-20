# wasm-dag-forge

A proof of concept for durable distributed job execution: batch jobs built from templates
(compile, simulate, aggregate), run as WASM on a fixed-size local Kubernetes cluster.

![System context](docs/architecture/01-system-context.drawio.svg)

## Worked example

This animation follows one job from submission to completion.

![One job, end to end](docs/animations/media/job-walkthrough.gif)

## Getting started

See [docs/dev-setup.md](docs/dev-setup.md).

## License

Licensed under the [Apache License 2.0](LICENSE).
