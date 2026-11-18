"""The identity of a submitted job: its key, its id, its classes and its fingerprint.

No key is ever generated, so a resent entry always derives the same job.
"""

from dataclasses import dataclass

import wdf_keys

from wdf_dispatchapi.contract import fingerprint
from wdf_dispatchapi.models import BatchEntry, BatchHeader


@dataclass(frozen=True)
class Identity:
    key: str
    job_id: str
    priority_class: str
    job_class: str
    fingerprint: str


def job_key(batch_key: str, entry: BatchEntry) -> str:
    """The entry's submission key, else `<batch key>/<index>`."""
    return entry.submission_key or f"{batch_key}/{entry.index}"


def batch_id(tenant: str, header: BatchHeader) -> str:
    return wdf_keys.batch_id(tenant, header.batch_key)


def identify(tenant: str, header: BatchHeader, entry: BatchEntry, manifest_sha256: str) -> Identity:
    """The identity of one entry, whose absent classes are the header's."""
    key = job_key(header.batch_key, entry)
    priority_class = entry.priority_class or header.priority_class
    job_class = entry.job_class or header.job_class
    return Identity(
        key=key,
        job_id=wdf_keys.job_id(tenant, key),
        priority_class=priority_class,
        job_class=job_class,
        fingerprint=fingerprint(
            job_class=job_class,
            manifest_sha256=manifest_sha256,
            parameters=entry.parameters,
            priority_class=priority_class,
            seed=entry.seed,
        ),
    )
