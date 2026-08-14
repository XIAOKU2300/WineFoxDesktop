from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from desktop_runtime.resource_seed import seed_bridge_source
from desktop_runtime.setup_manifest import load_setup_manifest
from desktop_runtime.setup_service import (
    EnvironmentStatus,
    ModelState,
    NodeState,
    Planner,
    Scanner,
)
from desktop_runtime.setup_python import PythonProbe


def make_root(base: Path, name: str = "ComfyUI") -> Path:
    root = base / name
    (root / "models" / "checkpoints").mkdir(parents=True)
    (root / "custom_nodes").mkdir(parents=True)
    (root / "main.py").write_text("pass\n", encoding="utf-8")
    return root


def _sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


class ScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_setup_manifest()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = make_root(self.base)
        self.scanner = Scanner(self.manifest)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_empty_root_reports_missing(self) -> None:
        status = self.scanner.scan(bases=[self.base])
        self.assertEqual(len(status.candidates), 1)
        self.assertIsNotNone(status.root)
        self.assertEqual(status.state_code, "repair_needed")
        self.assertTrue(all(not n.present for n in status.nodes.values()))
        self.assertTrue(all(not m.present for m in status.models.values()))
        self.assertFalse(status.bridge.installed)
        self.assertFalse(status.python.found)
        self.assertEqual(len(status.missing_model_ids), 19)

    def test_present_model_detected(self) -> None:
        # depth_anything_v2_vitl.pth -> min_gb 1.0; use a sparse 1 GiB file.
        _sparse(self.root / "custom_nodes" / "comfyui_controlnet_aux" / "ckpts" / "depth-anything" / "Depth-Anything-V2-Large" / "depth_anything_v2_vitl.pth", 1 << 30)
        status = self.scanner.scan(bases=[self.base])
        core = status.models["core.depth_anything_v2_vitl"]
        self.assertTrue(core.present)
        self.assertFalse(core.corrupt)
        self.assertNotIn("core.depth_anything_v2_vitl", status.missing_model_ids)

    def test_corrupt_model_detected(self) -> None:
        _sparse(self.root / "models" / "checkpoints" / "Illustrious-XL-v2.0.safetensors", 1024)
        status = self.scanner.scan(bases=[self.base])
        model = status.models["minimal.illustrious_xl_v2"]
        self.assertTrue(model.present)
        self.assertTrue(model.corrupt)
        self.assertIn("minimal.illustrious_xl_v2", status.missing_model_ids)

    def test_bridge_and_node_detected(self) -> None:
        seed_bridge_source(self.root / "custom_nodes")
        fake = self.root / "custom_nodes" / "ComfyUI-GGUF"
        fake.mkdir()
        (fake / "__init__.py").write_text("", encoding="utf-8")
        status = self.scanner.scan(bases=[self.base])
        self.assertTrue(status.bridge.installed)
        self.assertEqual(status.bridge.version, "1.4.2")
        self.assertTrue(status.nodes["wan.gguf"].present)


class StateCodeTests(unittest.TestCase):
    def test_no_root(self) -> None:
        status = EnvironmentStatus()
        self.assertEqual(status.state_code, "no_root")

    def test_ready_when_complete(self) -> None:
        manifest = load_setup_manifest()
        status = EnvironmentStatus(root=Path(r"C:\ComfyUI"))
        for node in manifest.nodes.values():
            status.nodes[node.id] = NodeState(id=node.id, present=True)
        status.bridge.installed = True
        for model in manifest.models.values():
            status.models[model.id] = ModelState(id=model.id, present=True, corrupt=False, size_bytes=1, min_gb=model.min_gb)
        status.python = PythonProbe(found=True)
        self.assertEqual(status.state_code, "ready")


class PlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_setup_manifest()
        cls.planner = Planner(cls.manifest)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = make_root(self.base)
        self.empty_status = Scanner(self.manifest).scan(bases=[self.base])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_lite_plan_steps(self) -> None:
        plan = self.planner.plan_for_profile(self.empty_status, "lite")
        kinds = [(s.kind, s.group) for s in plan.steps]
        self.assertEqual(kinds, [("nodes", "minimal"), ("models", "core"), ("bridge", None)])
        nodes_step = plan.steps[0]
        self.assertEqual(len(nodes_step.items), 3)
        self.assertEqual(nodes_step.pip_packages, ("ultralytics",))
        self.assertEqual(len(plan.steps[1].items), 1)
        model_item = plan.steps[1].items[0]
        self.assertEqual(model_item.kind, "model")
        self.assertEqual(model_item.spec.id, "core.depth_anything_v2_vitl")
        # Bridge source host is allow-listed.
        for s in model_item.spec.sources:
            self.assertIn(s.host, self.manifest.bridge.allowed_hosts)

    def test_complete_plan_excludes_wan_quality(self) -> None:
        plan = self.planner.plan_for_profile(self.empty_status, "complete")
        model_groups = [s.group for s in plan.steps if s.kind == "models"]
        self.assertIn("wan", model_groups)
        self.assertIn("wan_clip_vision", model_groups)
        self.assertNotIn("wan_quality", model_groups)

    def test_only_missing_skips_installed_bridge(self) -> None:
        seed_bridge_source(self.root / "custom_nodes")
        status = Scanner(self.manifest).scan(bases=[self.base])
        plan = self.planner.plan_for_profile(status, "lite", only_missing=True)
        self.assertFalse(any(s.kind == "bridge" for s in plan.steps))

    def test_repair_plan_covers_all_groups(self) -> None:
        plan = self.planner.plan_for_repair(self.empty_status)
        kinds = {s.kind for s in plan.steps}
        self.assertIn("bridge", kinds)
        self.assertIn("nodes", kinds)
        self.assertIn("models", kinds)
        # Every model group with missing files appears exactly once.
        model_groups = [s.group for s in plan.steps if s.kind == "models"]
        self.assertEqual(set(model_groups), set(self.manifest.model_groups))

    def test_custom_plan(self) -> None:
        plan = self.planner.plan_for_custom(self.empty_status, [("models", "wan"), ("bridge", None)])
        kinds = [(s.kind, s.group) for s in plan.steps]
        self.assertEqual(kinds, [("models", "wan"), ("bridge", None)])
        self.assertEqual(len(plan.steps[0].items), 4)

    def test_retry_plan(self) -> None:
        base = self.planner.plan_for_profile(self.empty_status, "full")
        retry = self.planner.plan_for_retry(self.empty_status, [1], base)
        self.assertEqual([s.kind for s in retry.steps], ["models"])

    def test_to_dict_has_no_absolute_path(self) -> None:
        plan = self.planner.plan_for_profile(self.empty_status, "lite")
        plan_json = plan.to_dict()
        status_json = self.empty_status.to_dict()
        text = str(plan_json) + str(status_json)
        self.assertNotIn(str(self.base), text)


if __name__ == "__main__":
    unittest.main()
