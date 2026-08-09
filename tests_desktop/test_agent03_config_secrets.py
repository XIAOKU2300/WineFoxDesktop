"""Agent 03 测试：paths / config / secrets / legacy_import。

覆盖：默认路径、中文/空格/长路径、路径逃逸、原子保存、损坏配置、备份、
public view 无密钥、内存 SecretStore、旧数据 dry-run、幂等、源目录只读。

约定：全部使用 tmp_path；绝不触碰真实 Credential Manager / LOCALAPPDATA。
"""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # winefox_core 为命名空间包，无需 __init__.py

from winefox_core.config import (  # noqa: E402
    BACKUP_FILENAME,
    ConfigError,
    ConfigStatus,
    SCHEMA_VERSION,
    SETTINGS_FILENAME,
    SettingsStore,
    _SECRET_KEY_RE,
)
from winefox_core.legacy_import import (  # noqa: E402
    LegacyImportPlan,
    build_plan,
    execute_plan,
)
from winefox_core.paths import (  # noqa: E402
    MANAGED_SUBDIRS,
    PathError,
    PathManager,
    default_root,
)
from winefox_core.secrets import (  # noqa: E402
    SECRET_STORE_FAILED,
    MemorySecretBackend,
    SecretStore,
    SecretStoreError,
)


# ===================================================================== paths
class TestPaths:
    def test_default_root_uses_localappdata(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
        assert default_root() == tmp_path / "AppData" / "Local" / "WineFox"
        pm = PathManager()
        assert pm.root == (tmp_path / "AppData" / "Local" / "WineFox").resolve()

    def test_default_root_falls_back_to_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert default_root() == tmp_path / ".winefox"

    def test_managed_subdirs_all_present(self, tmp_path):
        pm = PathManager(tmp_path / "root")
        subdirs = pm.managed_subdirs()
        assert set(subdirs) == set(MANAGED_SUBDIRS)
        assert len(MANAGED_SUBDIRS) == 9
        for name in MANAGED_SUBDIRS:
            assert subdirs[name] == pm.root / name

    def test_chinese_space_long_paths(self, tmp_path):
        long_name = "酒狐-测试 " + "非常长的目录名" * 20 + " .end"
        root = tmp_path / "WineFox 数据" / long_name
        pm = PathManager(root)
        pm.ensure_dirs()
        for name in MANAGED_SUBDIRS:
            assert (root / name).is_dir()
        rel = "工作流 目录/角色 檔案/角色名_很长" + "长" * 80 + ".json"
        joined = pm.resolve_managed("workflows", rel)
        assert joined.is_relative_to(pm.workflow_dir)
        assert ".." not in joined.parts

    def test_path_escape_rejected(self, tmp_path):
        pm = PathManager(tmp_path / "root")
        for evil in ("../evil", "a/../../evil", "..", "../../etc/passwd"):
            with pytest.raises(PathError):
                pm.resolve_managed("data", evil)
        with pytest.raises(PathError):
            pm.resolve_managed("data", str(tmp_path / "outside"))  # 绝对路径
        with pytest.raises(PathError):
            pm.resolve_managed("data", "C:/Windows/win.ini")
        with pytest.raises(PathError):
            pm.resolve_managed("data", "~/secret")
        with pytest.raises(PathError):
            pm.resolve_managed("not_a_subdir", "x")  # 未知受管目录

    def test_resolve_managed_relative_ok(self, tmp_path):
        pm = PathManager(tmp_path / "root")
        assert pm.resolve_managed("uploads", "sub/2024/x.png") == (
            pm.root / "uploads" / "sub" / "2024" / "x.png"
        )
        assert pm.resolve_managed("uploads") == pm.upload_dir

    def test_install_dir_readonly(self, tmp_path):
        install = tmp_path / "install"
        (install / "winefox_core").mkdir(parents=True)
        pm = PathManager(tmp_path / "root", install_dir=install)
        assert pm.is_readonly(install)
        assert pm.is_readonly(install / "winefox_core" / "paths.py")
        assert not pm.is_readonly(pm.root)
        assert not pm.writable(install / "x.txt")
        assert pm.writable(pm.root / "config")
        # root 不在 install 内时 ensure_dirs 正常工作
        pm.ensure_dirs()
        assert (pm.root / "config").is_dir()

    def test_root_under_install_never_created(self, tmp_path):
        install = tmp_path / "install"
        install.mkdir()
        pm = PathManager(install / "config", install_dir=install)
        with pytest.raises(PathError):
            pm.ensure_dirs()


# ===================================================================== config
def _store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "config")


class TestConfig:
    def test_not_found_returns_defaults(self, tmp_path):
        result = _store(tmp_path).load()
        assert result.status is ConfigStatus.NOT_FOUND
        defaults = _store(tmp_path).require()
        assert defaults["comfyui"]["address"] == "127.0.0.1:8188"
        assert defaults["model_family"] == "illustrious"
        assert defaults["size"]["width"] == 832

    def test_save_load_roundtrip_and_schema(self, tmp_path):
        store = _store(tmp_path)
        store.update(
            {
                "comfyui": {"address": "10.0.0.8:8188", "tls": True, "auth_ref": "comfyui_api"},
                "model_family": "anima",
                "ui": {"language": "en-US"},
                "queue": {"max_concurrent": 2},
            }
        )
        result = store.load()
        assert result.status is ConfigStatus.OK
        cfg = result.config
        assert cfg["schemaVersion"] == SCHEMA_VERSION == 1
        assert cfg["comfyui"]["address"] == "10.0.0.8:8188"
        assert cfg["comfyui"]["tls"] is True
        assert cfg["comfyui"]["auth_ref"] == "comfyui_api"
        assert cfg["model_family"] == "anima"
        assert cfg["ui"]["language"] == "en-US"
        assert cfg["queue"]["max_concurrent"] == 2
        assert cfg["size"]["upscale"] == 1.5  # 未改动的保持默认

    def test_atomic_save_no_tmp_left_and_backup_kept(self, tmp_path):
        store = _store(tmp_path)
        store.update({"model_family": "anima"})
        first = store.path.read_text(encoding="utf-8")
        assert not store.path.with_name(SETTINGS_FILENAME + ".tmp").exists()

        store.update({"model_family": "illustrious"})
        assert store.backup_path.exists()
        assert json.loads(store.backup_path.read_text(encoding="utf-8"))["model_family"] == "anima"
        assert json.loads(store.path.read_text(encoding="utf-8"))["model_family"] == "illustrious"
        # 备份内容与第一次保存一致（旧文件原样成为 .bak）
        assert store.backup_path.read_text(encoding="utf-8") == first

    def test_corrupt_config_returns_invalid_and_never_overwritten(self, tmp_path):
        store = _store(tmp_path)
        store.config_dir.mkdir(parents=True)
        store.path.write_text("{ this is not json !!!", encoding="utf-8")
        result = store.load()
        assert result.status is ConfigStatus.CONFIG_INVALID
        # load 不覆盖
        assert store.path.read_text(encoding="utf-8") == "{ this is not json !!!"
        # save 默认拒绝覆盖损坏文件
        with pytest.raises(ConfigError, match="拒绝覆盖损坏"):
            store.save({"model_family": "anima"})
        assert store.path.read_text(encoding="utf-8") == "{ this is not json !!!"
        # force 显式修复可行
        store.save({"model_family": "anima"}, force=True)
        assert store.load().config["model_family"] == "anima"

    def test_corrupt_top_level_and_bad_schema(self, tmp_path):
        store = _store(tmp_path)
        store.config_dir.mkdir(parents=True)
        store.path.write_text("[1,2,3]", encoding="utf-8")
        assert store.load().status is ConfigStatus.CONFIG_INVALID
        store.path.write_text(json.dumps({"schemaVersion": 99}), encoding="utf-8")
        assert store.load().status is ConfigStatus.UNSUPPORTED_SCHEMA

    def test_recover_from_backup(self, tmp_path):
        store = _store(tmp_path)
        store.update({"model_family": "anima"})
        store.update({"model_family": "illustrious"})  # 第二次保存产生 .bak
        assert store.backup_path.exists()
        store.path.write_text("broken{", encoding="utf-8")
        assert store.load().status is ConfigStatus.CONFIG_INVALID
        assert store.recover_from_backup() is True
        # .bak 语义：上一版完好配置（anima），恢复后配置可用
        assert store.load().config["model_family"] == "anima"

    def test_unknown_keys_dropped_shape_checked(self, tmp_path):
        store = _store(tmp_path)
        store.update({"comfyui": {"address": "x:1"}, "not_a_real_key": 42})
        cfg = store.load().config
        assert "not_a_real_key" not in cfg
        with pytest.raises(ConfigError):
            store.save({"comfyui": {"address": 12345}})

    def test_denies_secret_values(self, tmp_path):
        store = _store(tmp_path)
        # 顶层 / 嵌套密钥一律拒绝
        for bad in (
            {"api_key": "sk-live-123"},
            {"comfyui": {"token": "tok-abc"}},
            {"gpt_magic": {"draw": {"api_key": "sk-2"}}},
            {"auth_refs": {"comfyui": "sk-abc"}},  # auth_refs 也必须是引用名
        ):
            with pytest.raises(ConfigError):
                store.save(bad)
        # 空值允许（视为未设置）
        store.save({"comfyui": {"auth_ref": None}, "gpt_magic": {"draw": {"api_key": ""}}})
        # 认证引用（指针）允许
        store.save({"comfyui": {"auth_ref": "comfyui_api"}})
        assert _SECRET_KEY_RE.search("api_key") and _SECRET_KEY_RE.search("auth_ref") is None

    def test_public_view_never_contains_secrets(self, tmp_path):
        store = _store(tmp_path)
        cfg = store.require()
        cfg["ui"]["admin_panel_token"] = "super-secret-token-1"
        cfg["comfyui"]["password"] = "p@ssw0rd"
        cfg["comfyui"]["auth_ref"] = "comfyui_api"  # 引用不是密钥
        view = store.public_view(cfg)
        text = json.dumps(view, ensure_ascii=False)
        assert "super-secret-token-1" not in text
        assert "p@ssw0rd" not in text
        assert view["ui"]["admin_panel_token"] is None
        assert view["comfyui"]["password"] is None
        assert view["comfyui"]["auth_ref"] == "comfyui_api"  # 引用保留
        # public_view 返回的是副本，不影响原对象
        assert cfg["ui"]["admin_panel_token"] == "super-secret-token-1"


# ===================================================================== secrets
class TestSecrets:
    def test_memory_store_set_get_has_delete(self):
        store = SecretStore(backend=MemorySecretBackend())
        assert store.has("comfyui_api") is False
        store.set("comfyui_api", "sk-abc123")
        assert store.has("comfyui_api") is True
        assert store.get("comfyui_api") == "sk-abc123"
        store.set("comfyui_api", "sk-new")  # 覆盖
        assert store.get("comfyui_api") == "sk-new"
        assert store.get("missing") is None
        assert store.delete("comfyui_api") is True
        assert store.has("comfyui_api") is False
        assert store.delete("comfyui_api") is False

    def test_memory_store_isolation(self):
        a = SecretStore(backend=MemorySecretBackend())
        b = SecretStore(backend=MemorySecretBackend())
        a.set("k", "v-a")
        assert b.get("k") is None

    def test_backend_failure_maps_to_secret_store_failed(self):
        class BrokenBackend:
            def set(self, name, value):
                raise OSError("disk full")

            def get(self, name):
                raise RuntimeError("boom")

            def delete(self, name):
                raise ValueError("no")

            def has(self, name):
                raise RuntimeError("boom")

        store = SecretStore(backend=BrokenBackend())
        for action in (
            lambda: store.set("k", "v"),
            lambda: store.get("k"),
            lambda: store.delete("k"),
            lambda: store.has("k"),
        ):
            with pytest.raises(SecretStoreError) as exc_info:
                action()
            assert exc_info.value.code == SECRET_STORE_FAILED

    def test_invalid_name_and_value(self):
        store = SecretStore(backend=MemorySecretBackend())
        with pytest.raises(SecretStoreError):
            store.set("", "v")
        with pytest.raises(SecretStoreError):
            store.set("k", 123)  # 非字符串
        with pytest.raises(SecretStoreError):
            store.get("")

    def test_logs_never_contain_values(self, caplog):
        import logging

        store = SecretStore(backend=MemorySecretBackend())
        secret_value = "sk-live-TOP-SECRET-9f8e"
        with caplog.at_level(logging.DEBUG, logger="winefox_core.secrets"):
            store.set("comfyui_api", secret_value)
            store.get("comfyui_api")
            store.has("comfyui_api")
        assert secret_value not in caplog.text
        assert "comfyui_api" in caplog.text  # 条目名可出现，值不可

    def test_service_name_constant(self):
        from winefox_core.secrets import SERVICE_NAME

        assert SERVICE_NAME == "WineFoxDesktop"

    def test_memory_backend_never_contacts_keyring(self):
        # 测试环境严禁触碰真实凭据库：默认构造在无 keyring 时会抛错而不是落盘明文
        try:
            SecretStore()
        except SecretStoreError:
            pass  # 允许不可用；绝不允许明文 fallback
        else:
            # keyring 可用 -> 默认后端必须是 keyring（不是文件/明文）
            assert "Keyring" in type(SecretStore().backend).__name__


# ===================================================================== legacy import
def _make_legacy_plugin_data(root: Path) -> Path:
    """构造一份逼真的 AstrBot plugin_data 目录，返回插件数据根。"""
    plugin = root / "data" / "plugin_data" / "astrbot_plugin_comfyui_pro_UltraPlusMax"
    (plugin / "workflow").mkdir(parents=True)
    (plugin / "favorites" / "鸣潮 椿").mkdir(parents=True)
    (plugin / "gpt_magic" / "history").mkdir(parents=True)

    (plugin / "workflow" / "finish_illustrious_api.json").write_text(
        json.dumps({"nodes": ["a", "b"], "name": "工作流 一"}), encoding="utf-8"
    )
    (plugin / "workflow" / "wan_i2v_api.json").write_text(
        json.dumps({"nodes": ["c"], "name": "wan video"}), encoding="utf-8"
    )
    (plugin / "lora_profiles.json").write_text(
        json.dumps({"椿": {"lora": "chun_v1.safetensors", "tags": "长直发"}}), encoding="utf-8"
    )
    (plugin / "lora_aliases_anima.json").write_text(
        json.dumps({"椿": "chun_v1"}), encoding="utf-8"
    )
    (plugin / "favorites" / "鸣潮 椿" / "chun_20250101.png").write_bytes(b"\x89PNG fake")
    (plugin / "favorites" / "鸣潮 椿" / "chun_20250101.png.json").write_text(
        json.dumps({"character": "椿", "image_sha256": "abc"}), encoding="utf-8"
    )
    (plugin / "saved_prompts.json").write_text(
        json.dumps({"saved": [{"title": "旧提示词"}]}), encoding="utf-8"
    )
    (plugin / "gpt_magic" / "history" / "2025-01-01.json").write_text(
        json.dumps({"records": [{"prompt": "x"}]}), encoding="utf-8"
    )
    (plugin / "gpt_magic" / "db.json").write_text(
        json.dumps(
            {
                "config": {
                    "draw": {"api_key": "sk-live-LEGACY-SECRET"},
                    "webui": {"token": "tok-legacy"},
                },
                "users": {},
                "history": [],
            }
        ),
        encoding="utf-8",
    )
    return plugin


class TestLegacyImport:
    def test_scan_recognizes_all_kinds(self, tmp_path):
        plugin = _make_legacy_plugin_data(tmp_path)
        plan = build_plan([plugin], dest_root=tmp_path / "dest")
        assert isinstance(plan, LegacyImportPlan)
        counts = plan.counts_by_kind()
        assert counts["workflow"] == 2
        assert counts["lora_profiles"] == 1
        assert counts["lora_aliases"] == 1
        assert counts["favorite"] == 2  # 图片 + 侧车
        assert counts["history"] == 2  # saved_prompts + gpt_magic/history
        assert counts["gpt_magic_config"] == 1
        assert plan.total_items == 9
        assert plan.total_bytes > 0
        assert plan.summary()["items"] == 9
        assert len(plan.batches) == 6
        # 批次 ID 确定性
        again = build_plan([plugin], dest_root=tmp_path / "dest")
        assert [b.batch_id for b in plan.batches] == [b.batch_id for b in again.batches]

    def test_dry_run_writes_nothing(self, tmp_path):
        plugin = _make_legacy_plugin_data(tmp_path)
        dest = tmp_path / "dest"
        plan = build_plan([plugin], dest_root=dest)
        report = execute_plan(plan, dest, dry_run=True)
        assert report.dry_run is True
        # dry-run 展示全部待办（含需确认项）
        assert len(report.copied) == plan.total_items == 9
        assert not dest.exists()  # dry-run 绝不落盘

    def test_execute_copies_and_is_idempotent(self, tmp_path):
        plugin = _make_legacy_plugin_data(tmp_path)
        dest = tmp_path / "dest"
        plan = build_plan([plugin], dest_root=dest)
        report1 = execute_plan(plan, dest, dry_run=False)
        assert len(report1.copied) == 8  # gpt_magic_config 需确认
        assert len(report1.skipped_unconfirmed) == 1
        copied_files = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
        assert "legacy/workflow/finish_illustrious_api.json" in copied_files
        assert "legacy/favorite/鸣潮 椿/chun_20250101.png" in copied_files
        # 幂等：第二次执行全部跳过，不新增文件
        report2 = execute_plan(plan, dest, dry_run=False)
        assert report2.copied == []
        assert len(report2.skipped_existing) == 8
        assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == copied_files
        # 幂等键稳定
        assert plan.idempotency_keys == build_plan([plugin], dest_root=dest).idempotency_keys

    def test_secrets_never_copied_without_confirmation(self, tmp_path):
        plugin = _make_legacy_plugin_data(tmp_path)
        dest = tmp_path / "dest"
        plan = build_plan([plugin], dest_root=dest)
        secret_item = plan.items_of("gpt_magic_config")[0]
        assert secret_item.requires_confirmation is True
        assert "config.draw.api_key" in secret_item.detail["secret_paths"]
        # 计划与摘要中绝不出现密钥值（只记录路径名）
        assert "sk-live-LEGACY-SECRET" not in json.dumps(secret_item.detail)
        assert "sk-live-LEGACY-SECRET" not in json.dumps(plan.summary())
        # 未确认 -> 不复制
        execute_plan(plan, dest, dry_run=False)
        assert not (dest / "legacy" / "gpt_magic_config").exists()
        # 确认后才复制（仍然只是复制 db.json 原样，不解析密钥）
        report = execute_plan(
            plan, dest, dry_run=False, confirmed_keys={secret_item.idempotency_key}
        )
        assert (dest / "legacy" / "gpt_magic_config" / "db.json").exists()
        # 复制出的文件可能含旧密钥（迁移语义：只复制不解析）——WineFox 不得把它当配置
        raw = json.loads(
            (dest / "legacy" / "gpt_magic_config" / "db.json").read_text(encoding="utf-8")
        )
        assert raw["config"]["draw"]["api_key"] == "sk-live-LEGACY-SECRET"

    def test_conflict_detection(self, tmp_path):
        plugin = _make_legacy_plugin_data(tmp_path)
        dest = tmp_path / "dest"
        plan = build_plan([plugin], dest_root=dest)
        assert plan.conflicts == []  # 空目标无冲突
        # 预放一个内容不同的同名文件
        target = dest / "legacy" / "workflow" / "finish_illustrious_api.json"
        target.parent.mkdir(parents=True)
        target.write_text("different", encoding="utf-8")
        plan2 = build_plan([plugin], dest_root=dest)
        conflict_items = plan2.conflicts
        assert len(conflict_items) == 1
        assert conflict_items[0].rel == "finish_illustrious_api.json"
        # 执行时默认跳过冲突
        report = execute_plan(plan2, dest, dry_run=False)
        assert len(report.skipped_conflict) == 1
        assert target.read_text(encoding="utf-8") == "different"  # 未被覆盖
        # overwrite_conflicts 且备份后覆盖
        report3 = execute_plan(plan2, dest, dry_run=False, overwrite_conflicts=True)
        assert any(e.existed_before and e.backup_of for e in report3.rollback.entries)

    def test_rollback_manifest_restores(self, tmp_path):
        plugin = _make_legacy_plugin_data(tmp_path)
        dest = tmp_path / "dest"
        plan = build_plan([plugin], dest_root=dest)
        execute_plan(plan, dest, dry_run=False)
        # 修改源文件，制造一个需要覆盖的冲突
        target = dest / "legacy" / "workflow" / "finish_illustrious_api.json"
        before = target.read_bytes()
        (plugin / "workflow" / "finish_illustrious_api.json").write_text(
            json.dumps({"nodes": ["changed"]}), encoding="utf-8"
        )
        plan2 = build_plan([plugin], dest_root=dest)
        assert len(plan2.conflicts) == 1
        report = execute_plan(plan2, dest, dry_run=False, overwrite_conflicts=True)
        assert target.read_bytes() != before
        from winefox_core.legacy_import import rollback_plan

        restored = rollback_plan(report)
        assert restored >= 1
        assert target.read_bytes() == before

    def test_source_dir_readonly_scan_and_copy(self, tmp_path):
        plugin = _make_legacy_plugin_data(tmp_path)
        if os.name != "nt":  # Windows 上 chmod 语义受限，跳过只读位测试
            for p in plugin.rglob("*"):
                if p.is_file():
                    p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            for d in sorted([p for p in plugin.rglob("*") if p.is_dir()], reverse=True):
                d.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        plan = build_plan([plugin], dest_root=tmp_path / "dest")
        assert plan.total_items == 9
        report = execute_plan(plan, tmp_path / "dest", dry_run=False)
        assert len(report.copied) == 8
        # 源目录一个文件都没少（只复制不移动）
        assert (plugin / "workflow" / "finish_illustrious_api.json").exists()

    def test_discover_source_roots(self, tmp_path, monkeypatch):
        plugin = _make_legacy_plugin_data(tmp_path)
        monkeypatch.delenv("WINFOX_LEGACY_PLUGIN_DATA", raising=False)
        from winefox_core.legacy_import import discover_source_roots

        found = discover_source_roots(cwd=tmp_path, env={})
        assert plugin in found
        # env 覆盖
        found2 = discover_source_roots(cwd=tmp_path, env={"WINFOX_LEGACY_PLUGIN_DATA": str(plugin)})
        assert found2 == [plugin]

    def test_legacy_ids_covered(self):
        from winefox_core.legacy_import import LEGACY_IDS

        assert "astrbot_plugin_comfyui_pro" in LEGACY_IDS
        assert "astrbot_plugin_comfyui" in LEGACY_IDS

    def test_no_sqlite_import(self):
        import winefox_core.legacy_import as mod

        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "import sqlite" not in source.lower()
        assert "sqlite3" not in source.lower()
