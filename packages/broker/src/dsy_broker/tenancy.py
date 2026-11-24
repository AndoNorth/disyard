"""The tenancy profiles, read through package data, for the publisher and the consumer of the intake
queues alike."""

from .contract import tenancy
from .models import TenancyProfile


def load(profile: str) -> TenancyProfile:
    """The tenancy profile of a preset, checked by the loader.

    Raises KeyError for a profile the file does not hold.
    """
    profiles = tenancy().profiles
    if profile not in profiles:
        known = ", ".join(profiles)
        raise KeyError(f"unknown tenancy profile {profile!r}, the profiles are {known}")
    return profiles[profile]
