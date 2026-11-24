"""Task placement, layer 2 scheduling: a stateless service that answers the workflow's `place` call
with a pool task queue and the activity timeouts from the pool table, and records each answer
beside a read-only snapshot of the pools."""
