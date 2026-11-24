"""The tenancy profiles: the typed loader, and the two rules that read repository files, which
keep the loader free of repository paths."""

from pathlib import Path

import pytest
import yaml
from dsy_broker.models import PRESETS, Tenancy
from dsy_broker.tenancy import load
from dsy_jobdoc.profile_files import load_profile

ROOT = Path(__file__).resolve().parents[3]
PINNED = ROOT / "contracts/tenancy/tenancy.yaml"
VECTORS = ROOT / "contracts/tenancy/vectors/tenancy"


def read(path: Path) -> Tenancy:
    return Tenancy.model_validate(yaml.safe_load(path.read_text()))


def limits_classes(name: str) -> list[str]:
    return load_profile(name, ROOT).classes


def job_class_mismatches(tenancy: Tenancy, classes_of=limits_classes) -> list[str]:
    """The profiles whose job class caps differ from the classes of the limits profile."""
    problems = []
    for name, profile in tenancy.profiles.items():
        classes = classes_of(name)
        if set(profile.caps.job_class) != set(classes):
            problems.append(f"{name}: caps {sorted(profile.caps.job_class)}, classes {classes}")
    return problems


def preset_mismatches(tenancy: Tenancy, root: Path) -> list[str]:
    """Differences between the profile names and the presets, and between the Tilt profiles."""
    presets = {p.stem for p in (root / "deploy/presets").glob("*.yaml")}
    tilt = set(yaml.safe_load((root / "deploy/tilt/profiles.yaml").read_text())["profiles"])
    problems = []
    if set(tenancy.profiles) != presets:
        problems.append(f"profiles {sorted(tenancy.profiles)}, presets {sorted(presets)}")
    if set(tenancy.tilt_profiles) != tilt:
        problems.append(f"tilt_profiles {sorted(tenancy.tilt_profiles)}, Tilt {sorted(tilt)}")
    return problems


def test_load_returns_the_typed_ci_profile():
    profile = load("ci")
    assert profile.priority_classes == ["standard"]
    assert list(profile.tenants) == ["acme"]
    figures = {
        "reserve_interval": profile.reserve_interval.value,
        "open": profile.caps.open.value,
        "global": profile.caps.global_.value,
        "short": profile.caps.job_class["short"].value,
        "long": profile.caps.job_class["long"].value,
        "acme": profile.tenants["acme"].quota.value,
    }
    assert figures == {
        "reserve_interval": 5,
        "open": 2000,
        "global": 1000,
        "short": 1000,
        "long": 100,
        "acme": 1000,
    }


def test_load_gives_dev_and_soak_the_ci_figures():
    assert load("dev") == load("ci") == load("soak")


def test_load_refuses_an_unknown_profile():
    with pytest.raises(KeyError, match="unknown tenancy profile 'permissive'"):
        load("permissive")


def test_every_figure_is_marked_as_an_estimate():
    for profile in read(PINNED).profiles.values():
        caps = profile.caps
        figures = [profile.reserve_interval, caps.open, caps.global_, *caps.job_class.values()]
        figures += [tenant.quota for tenant in profile.tenants.values()]
        assert all(figure.estimate for figure in figures)


def test_tenancy_job_classes_match_limits():
    assert job_class_mismatches(read(PINNED)) == []
    # The limits model of job v2 takes exactly short and long, so a gained class arrives with the
    # next job version, and this stands in for its limits profile.
    gained = {**{name: limits_classes(name) for name in PRESETS}, "ci": ["short", "long", "huge"]}
    assert job_class_mismatches(read(PINNED), gained.__getitem__) == [
        "ci: caps ['long', 'short'], classes ['short', 'long', 'huge']"
    ]


def test_tenancy_profiles_match_presets():
    assert preset_mismatches(read(PINNED), ROOT) == []
    staging = read(VECTORS / "invalid/unknown-profile/staging-profile.yaml")
    assert preset_mismatches(staging, ROOT) == [
        "profiles ['ci', 'dev', 'soak', 'staging'], presets ['ci', 'dev', 'soak']"
    ]
