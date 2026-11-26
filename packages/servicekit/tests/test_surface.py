import importlib
import re
from pathlib import Path

import pytest

README = (Path(__file__).parents[1] / "README.md").read_text()


def _listed():
    return {
        module: re.findall(r"`(\w+)`", names)
        for module, names in re.findall(r"^- `(dsy_servicekit\.\w+)`: (.+)$", README, re.M)
    }


@pytest.mark.parametrize("module", ["config", "lifecycle", "pg"])
def test_all_equals_the_readme_list(module):
    name = f"dsy_servicekit.{module}"
    assert sorted(importlib.import_module(name).__all__) == sorted(_listed()[name])


def test_readme_lists_only_these_modules():
    assert set(_listed()) == {f"dsy_servicekit.{m}" for m in ("config", "lifecycle", "pg")}
