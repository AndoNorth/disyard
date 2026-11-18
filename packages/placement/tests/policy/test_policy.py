"""The placement policy: the static placer over the pool table, answered from one module."""

import ast
import pkgutil
from pathlib import Path

import wdf_placement
from wdf_placement import policy
from wdf_workflow.placement import Placement, PlacementRequest, task_pools
from wdf_workflow.testing import placer_contract

# What decides an answer: the static placer, the pool table and the task pool registry, and an
# answer built by hand.
RULES = {"StaticPlacer", "pools", "task_pools"}
ANSWERS = {"Placement", "Unschedulable"}


class FixedQueue:
    def place(self, request: PlacementRequest, escalate: bool = False) -> Placement:
        return Placement(
            queue="wdf-fixed",
            memory_class="64Mi",
            escalated=False,
            schedule_to_start_s=1,
            start_to_close_s=request.run_timeout_s + 1,
            heartbeat_s=1,
        )


def _modules() -> dict[str, ast.Module]:
    package = Path(wdf_placement.__file__).parent
    return {
        info.name: ast.parse((package / f"{info.name}.py").read_text())
        for info in pkgutil.iter_modules([str(package)])
    }


def _breaches(name: str, tree: ast.Module) -> list[str]:
    breaches = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported = {alias.name for alias in node.names}
            if imported & RULES:
                breaches.append(f"{name} imports {sorted(imported & RULES)}")
            if (node.module or "").endswith("policy") and "policy" in imported:
                breaches.append(f"{name} binds policy() at import, so a patch would miss it")
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) in ANSWERS:
            breaches.append(f"{name} builds a {node.func.id} answer")
    return breaches


def test_policy_passes_placer_contract():
    placer_contract(policy.policy())


def test_policy_is_one_module(monkeypatch):
    """A policy patched to answer a fixed queue is what every answer comes from, and no other
    module holds a placement rule or binds the policy before the patch."""
    monkeypatch.setattr(policy, "policy", FixedQueue)
    for task_type in task_pools():
        request = PlacementRequest(task_type=task_type, cpu_milli=1, memory_mib=1, run_timeout_s=60)
        assert wdf_placement.policy.policy().place(request).queue == "wdf-fixed"
    modules = _modules()
    assert "policy" in modules and "snapshot" in modules
    breaches = [
        b for name, tree in modules.items() if name != "policy" for b in _breaches(name, tree)
    ]
    assert not breaches, breaches
    assert _breaches("policy", modules["policy"]) == ["policy imports ['StaticPlacer']"]
