set shell := ["bash", "-euo", "pipefail", "-c"]

import 'just/cluster.just'
import 'just/quality.just'
import 'just/tooling.just'
import 'just/acceptance.just'
import 'just/verify.just'
import 'just/seam.just'

[private]
default:
    @just --list
