"""Every rule file passes `promtool check rules` and every test file passes `promtool test rules`.

Rule files and test files are found by glob, so a new area adds files without touching this test."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

RULES = Path(__file__).resolve().parents[4] / "deploy/obs/prometheus/rules"
RULE_FILES = sorted(RULES.glob("*.yaml"))
TEST_FILES = sorted((RULES / "tests").glob("*.yaml"))


def promtool(*args: Path | str) -> str:
    exe = shutil.which("promtool")
    assert exe, "promtool is not on PATH, run through `nix develop -c`"
    done = subprocess.run([exe, *map(str, args)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout + done.stderr


def test_rule_files_exist() -> None:
    assert RULE_FILES and TEST_FILES


@pytest.mark.parametrize("path", RULE_FILES, ids=lambda p: p.name)
def test_check_rules(path: Path) -> None:
    assert "SUCCESS" in promtool("check", "rules", path)


@pytest.mark.parametrize("path", TEST_FILES, ids=lambda p: p.name)
def test_test_rules(path: Path) -> None:
    assert "SUCCESS" in promtool("test", "rules", path)


@pytest.mark.parametrize("path", RULE_FILES, ids=lambda p: p.name)
def test_every_alert_is_tested_and_has_a_severity(path: Path) -> None:
    alerts = {
        r["alert"]: r
        for g in yaml.safe_load(path.read_text())["groups"]
        for r in g["rules"]
        if "alert" in r
    }
    tested = " ".join(p.read_text() for p in TEST_FILES)
    for name, rule in alerts.items():
        assert rule["labels"]["severity"] in {"critical", "warning", "info"}, name
        assert f"alertname: {name}" in tested, name
