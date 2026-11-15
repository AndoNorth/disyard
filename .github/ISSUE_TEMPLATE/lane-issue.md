---
name: Lane issue
about: One unit of work from an approved epic breakdown spec
title: "[#N][lane] <verb phrase>"
---

<!--
Labels: epic:N, lane:<x>, tier:T0|T1|T2
Parent: #N
Spec: docs/delivery/epics/NN-<slug>.md#<anchor>
Acceptance criteria rules: each one names a command or test and an observable result.
"Works" and "is documented" are not criteria.
A criterion that cannot run yet says which tier it waits for, and the issue is blocked or split, never weakened.
-->

## Scope

<what, in 2 to 5 lines>

## Owns (exclusive)

<path globs. Touching a path outside this list means stop and comment>

## Consumes / produces

<contract versions consumed, stubs used, contract or artifact produced with its path>

## Acceptance criteria (each is observable)

- [ ] <criterion, with the command or test that shows it>

## Verifier

<named test file or command>

## Needs / blocks

<issue links, merge order within the lane>

## Done means

- [ ] `just ci` green on the branch
- [ ] the named tests or commands ran, and their real output (not a description of it) is pasted in the PR or linked as a CI run url
- [ ] nothing outside "Owns" changed
