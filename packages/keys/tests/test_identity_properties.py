"""Determinism, boundary, domain and collision properties of the identity keys."""

import itertools
import os
import subprocess
import sys

import wft_keys
from hypothesis import given
from hypothesis import strategies as st

text = st.text(max_size=20)
JOB = wft_keys.job_id("acme", "sub-1")


def test_deterministic_across_interpreters_with_different_hash_seeds():
    program = (
        "import wft_keys;"
        "print(wft_keys.job_id('acme','sub-1'),"
        "wft_keys.batch_id('acme','sub-1'),"
        "wft_keys.cloudevent_id('j','t','r0a1','placed',2))"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", program],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("0", "1", "4242")
    }
    assert len(outputs) == 1


@given(text, text, text, text)
def test_field_boundary_never_collides(a, b, c, d):
    if (a, b) != (c, d):
        assert wft_keys.job_id(a, b) != wft_keys.job_id(c, d)


@given(text, text)
def test_job_and_batch_keys_with_the_same_text_differ(tenant, key):
    assert wft_keys.job_id(tenant, key) != wft_keys.batch_id(tenant, key)


@given(text, text, text)
def test_tenants_are_distinct(first, second, key):
    if first != second:
        assert wft_keys.job_id(first, key) != wft_keys.job_id(second, key)


@given(
    text,
    text,
    text,
    st.sampled_from(["accepted", "placed", "attempt.failed"]),
    st.integers(min_value=0, max_value=50),
)
def test_cloudevent_id_never_contains_the_store_key_separator(job, task, attempt, kind, sequence):
    assert "#" not in wft_keys.cloudevent_id(job, task, attempt, kind, sequence)
    assert "#" not in wft_keys.job_id(job, task)


def test_workflow_id_is_the_job_id_with_a_prefix():
    assert wft_keys.workflow_id(JOB) == f"job-{JOB}"


def test_job_id_is_a_version_5_uuid():
    assert JOB[14] == "5"


def test_attempt_restart_no_collision():
    pairs = list(itertools.product(range(4), range(1, 6)))
    segments = {wft_keys.attempt_identity(attempt, resume) for resume, attempt in pairs}
    ids = {
        wft_keys.cloudevent_id(JOB, "compile", wft_keys.attempt_identity(attempt, resume), "k", 0)
        for resume, attempt in pairs
    }
    assert len(pairs) == 20
    assert len(segments) == len(ids) == 20


def test_pause_resume_pause_of_one_job_repeats_no_source_and_id():
    emitted = [
        ("/wft/control", wft_keys.cloudevent_id(JOB, "", "", kind, sequence))
        for sequence, kind in enumerate(
            ["pause.requested", "resume.requested", "pause.requested"], start=1
        )
    ]
    assert len(set(emitted)) == 3
