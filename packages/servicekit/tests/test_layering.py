import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "src" / "dsy_servicekit"

pytestmark = pytest.mark.seam


def _imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def test_no_import_of_another_dsy_package():
    own = "dsy_servicekit"
    offenders = {
        f"{path.name}: {name}"
        for path in SRC.rglob("*.py")
        for name in _imports(path)
        if name.split(".")[0].startswith("dsy_") and name.split(".")[0] != own
    }
    assert not offenders


def test_importing_loads_no_other_dsy_package():
    code = (
        "import sys, dsy_servicekit.config, dsy_servicekit.lifecycle, dsy_servicekit.pg;"
        "print(sorted({m.split('.')[0] for m in sys.modules if m.startswith('dsy_')}))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "['dsy_servicekit']"
