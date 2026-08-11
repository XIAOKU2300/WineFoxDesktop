from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from desktop_runtime.setup_errors import SetupError, SetupErrorCode
from desktop_runtime.setup_paths import (
    find_candidates,
    is_inside,
    is_root_drive,
    is_system_dir,
    safe_comfyui_path,
    test_root as is_comfyui_root,  # 别名避免 pytest 将其收集为测试（test_* 前缀）
    validate_comfyui_root,
)


def make_comfyui_root(base: Path, name: str = "ComfyUI") -> Path:
    root = base / name
    (root / "models" / "checkpoints").mkdir(parents=True)
    (root / "custom_nodes").mkdir(parents=True)
    (root / "main.py").write_text("pass\n", encoding="utf-8")
    return root


class TestRootTests(unittest.TestCase):
    def test_valid_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_comfyui_root(Path(tmp))
            self.assertTrue(is_comfyui_root(root))

    def test_models_only_is_not_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ComfyUI"
            (root / "models").mkdir(parents=True)
            self.assertFalse(is_comfyui_root(root))

    def test_main_py_plus_models_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ComfyUI"
            (root / "models").mkdir(parents=True)
            (root / "main.py").write_text("", encoding="utf-8")
            self.assertTrue(is_comfyui_root(root))


class ValidateRootTests(unittest.TestCase):
    def test_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_comfyui_root(Path(tmp))
            result = validate_comfyui_root(root)
            self.assertTrue(result.is_absolute())

    def test_missing_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SetupError) as ctx:
                validate_comfyui_root(Path(tmp) / "nope")
            self.assertEqual(ctx.exception.code, SetupErrorCode.ROOT_INVALID)

    def test_drive_root(self) -> None:
        drive = Path.cwd().anchor
        with self.assertRaises(SetupError) as ctx:
            validate_comfyui_root(drive)
        self.assertEqual(ctx.exception.code, SetupErrorCode.ROOT_INVALID)

    def test_system_dir(self) -> None:
        with self.assertRaises(SetupError) as ctx:
            validate_comfyui_root(Path(r"C:\Windows\System32"))
        self.assertEqual(ctx.exception.code, SetupErrorCode.ROOT_INVALID)

    def test_inside_install_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_comfyui_root(base)
            with self.assertRaises(SetupError) as ctx:
                validate_comfyui_root(root, install_dir=base)
            self.assertEqual(ctx.exception.code, SetupErrorCode.ROOT_READ_ONLY)

    def test_not_comfyui(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SetupError) as ctx:
                validate_comfyui_root(Path(tmp))
            self.assertEqual(ctx.exception.code, SetupErrorCode.ROOT_INVALID)

    def test_non_ascii_path_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_comfyui_root(base, name="绘世启动器ComfyUI")
            result = validate_comfyui_root(root)
            self.assertEqual(result, root.resolve())


class FindCandidatesTests(unittest.TestCase):
    def test_finds_portable_and_launcher_and_subdirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            direct = make_comfyui_root(base, "ComfyUI")
            # Portable layout: the wrapper dir is NOT a root; only the nested
            # ComfyUI carries models/main.py.
            portable = base / "ComfyUI_windows_portable"
            nested = portable / "ComfyUI"
            (nested / "models").mkdir(parents=True)
            (nested / "custom_nodes").mkdir(parents=True)
            (nested / "main.py").write_text("", encoding="utf-8")
            launcher = base / "A绘世启动器" / "ComfyUI"
            (launcher / "models").mkdir(parents=True)
            (launcher / "main.py").write_text("", encoding="utf-8")
            stray = make_comfyui_root(base, "SomeRandomComfy")

            found = find_candidates([base])
            resolved = {p.resolve() for p in found}
            self.assertIn(direct.resolve(), resolved)
            self.assertIn(nested.resolve(), resolved)
            self.assertNotIn(portable.resolve(), resolved)
            self.assertIn(launcher.resolve(), resolved)
            self.assertIn(stray.resolve(), resolved)

    def test_rejects_install_dir_inside_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Install dir itself is a valid-looking ComfyUI root.
            install = make_comfyui_root(base, "WineFox")
            install_copy = make_comfyui_root(base, "WineFoxCopy")
            found = find_candidates([base], install_dir=install)
            resolved = {p.resolve() for p in found}
            self.assertNotIn(install.resolve(), resolved)
            self.assertIn(install_copy.resolve(), resolved)


class SafeJoinTests(unittest.TestCase):
    def test_join_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = safe_comfyui_path(root, "models/checkpoints/x.safetensors")
            self.assertEqual(target, root / "models" / "checkpoints" / "x.safetensors")

    def test_rejects_escape(self) -> None:
        root = Path(r"C:\tmp\ComfyUI")
        with self.assertRaises(SetupError):
            safe_comfyui_path(root, "../evil.safetensors")

    def test_rejects_absolute_and_drive(self) -> None:
        root = Path(r"C:\tmp\ComfyUI")
        with self.assertRaises(SetupError):
            safe_comfyui_path(root, "C:/evil.safetensors")
        with self.assertRaises(SetupError):
            safe_comfyui_path(root, "/evil.safetensors")

    def test_rejects_traversal_via_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "ComfyUI"
            (root / "models").mkdir(parents=True)
            outside = base / "outside"
            outside.mkdir()
            link = root / "models" / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation not permitted on this host")
            with self.assertRaises(SetupError):
                safe_comfyui_path(root, "models/escape/evil.safetensors")


class HelperTests(unittest.TestCase):
    def test_is_system_dir(self) -> None:
        self.assertTrue(is_system_dir(r"C:\Windows"))
        self.assertTrue(is_system_dir(r"C:\Program Files"))
        self.assertFalse(is_system_dir(r"D:\ComfyUI"))

    def test_is_root_drive(self) -> None:
        self.assertTrue(is_root_drive(Path.cwd().anchor))
        self.assertFalse(is_root_drive(Path.cwd()))

    def test_is_inside(self) -> None:
        self.assertTrue(is_inside(r"C:\a\b\c", r"C:\a"))
        self.assertFalse(is_inside(r"C:\a\b\c", r"C:\x"))


if __name__ == "__main__":
    unittest.main()
