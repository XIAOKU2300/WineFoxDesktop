from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from desktop_runtime.resource_seed import (
    BridgeSourceError,
    copy_bridge_source,
    parse_bridge_version,
    resolve_bridge_source,
    seed_bridge_source,
    validate_bridge_dir,
)


def _fake_bridge(root: Path) -> Path:
    src = root / "Bridge"
    (src / "sub").mkdir(parents=True)
    (src / "__init__.py").write_text(
        'BRIDGE_VERSION = "9.9.9"\n', encoding="utf-8"
    )
    (src / "config.sample.json").write_text("{}", encoding="utf-8")
    (src / "sub" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "init.cpython-311.pyc").write_bytes(b"\x00")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return src


class BridgeSourceResolutionTests(unittest.TestCase):
    def test_bundled_source_resolves_and_is_valid(self) -> None:
        src = resolve_bridge_source()
        self.assertTrue(src.is_dir())
        version = validate_bridge_dir(src)
        self.assertEqual(version, "1.4.1")

    def test_parse_version_from_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            init = Path(tmp) / "__init__.py"
            init.write_text('BRIDGE_VERSION = "1.2.3"\n', encoding="utf-8")
            self.assertEqual(parse_bridge_version(init), "1.2.3")

    def test_parse_version_missing_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            init = Path(tmp) / "__init__.py"
            init.write_text("no version here\n", encoding="utf-8")
            with self.assertRaises(BridgeSourceError):
                parse_bridge_version(init)


class BridgeCopyTests(unittest.TestCase):
    def test_copy_excludes_git_and_pycache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = _fake_bridge(root)
            dest = root / "out" / "ComfyUI-AstrBot-LoraBridge"
            result = copy_bridge_source(src, dest)
            self.assertEqual(result.version, "9.9.9")
            self.assertTrue((dest / "__init__.py").is_file())
            self.assertTrue((dest / "sub" / "helper.py").is_file())
            self.assertEqual(result.files_copied, 3)  # __init__, sample, helper
            self.assertFalse((dest / ".git").exists())
            self.assertFalse((dest / "__pycache__").exists())
            self.assertTrue(result.total_bytes > 0)

    def test_seed_into_custom_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom_nodes = Path(tmp) / "custom_nodes"
            custom_nodes.mkdir()
            result = seed_bridge_source(custom_nodes)
            self.assertTrue((custom_nodes / "ComfyUI-AstrBot-LoraBridge" / "__init__.py").is_file())
            self.assertEqual(result.version, "1.4.1")

    def test_seed_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom_nodes = Path(tmp) / "custom_nodes"
            target = custom_nodes / "ComfyUI-AstrBot-LoraBridge"
            target.mkdir(parents=True)
            (target / "__init__.py").write_text("user data", encoding="utf-8")
            with self.assertRaises(BridgeSourceError):
                seed_bridge_source(custom_nodes)

    def test_invalid_source_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BridgeSourceError):
                copy_bridge_source(Path(tmp) / "missing", Path(tmp) / "out")


class BridgeSymlinkGuardTests(unittest.TestCase):
    def test_copy_refuses_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = _fake_bridge(root)
            outside = root / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            link = src / "evil_link.txt"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation not permitted on this host")
            with self.assertRaises(BridgeSourceError):
                copy_bridge_source(src, root / "out")


if __name__ == "__main__":
    unittest.main()
