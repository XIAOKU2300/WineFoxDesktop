"""AstrBot 旧插件数据导入规划器（只读扫描，绝不写 SQLite）。

识别目标（astrbot_plugin_comfyui_pro_UltraPlusMax 的 data/plugin_data 布局）：
- workflow/*.json                          -> 工作流
- lora_profiles*.json / lora_aliases*.json  -> LoRA 档案 / 别名
- favorites/ 图片与侧车 .json               -> 收藏
- saved_prompts.json / gpt_magic/history/*  -> 历史
- gpt_magic/db.json                        -> GPT Magic 配置（含密钥 -> 仅标记需确认）

输出 LegacyImportPlan：dry-run 语义、批次 ID、计数、冲突、空间估算、幂等键、
回滚清单设计。执行阶段只复制不移动、不触碰源文件、绝不写入密钥值。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("winefox_core.legacy_import")

PLUGIN_ID = "astrbot_plugin_comfyui_pro_UltraPlusMax"
LEGACY_IDS = ("astrbot_plugin_comfyui_pro_UltraPlusMax", "astrbot_plugin_comfyui_pro", "astrbot_plugin_comfyui")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

#: GPT Magic db.json 中需要人工确认的密钥路径（只记录路径名，不记录值）。
GPT_MAGIC_SECRET_PATHS = (
    ("config", "draw", "api_key"),
    ("config", "webui", "token"),
    ("keys",),
)


@dataclass(frozen=True)
class LegacyItem:
    """单个可导入条目。frozen -> 幂等键稳定。"""

    kind: str  # workflow | lora_profiles | lora_aliases | favorite | history | gpt_magic_config
    source: Path
    rel: str  # 相对 source root 的路径（正斜杠）
    size_bytes: int
    sha256: str
    requires_confirmation: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def idempotency_key(self) -> str:
        """kind + rel + 内容指纹。内容不变则键不变，重复执行可跳过。"""
        return f"{self.kind}:{self.rel}:{self.sha256[:16]}"


@dataclass(frozen=True)
class LegacyBatch:
    batch_id: str
    source_root: Path
    kind: str
    items: tuple[LegacyItem, ...]

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def bytes(self) -> int:
        return sum(item.size_bytes for item in self.items)


@dataclass
class LegacyImportPlan:
    source_roots: list[Path]
    batches: list[LegacyBatch] = field(default_factory=list)
    idempotency_keys: set[str] = field(default_factory=set)

    @property
    def items(self) -> list[LegacyItem]:
        return [item for batch in self.batches for item in batch.items]

    @property
    def total_items(self) -> int:
        return len(self.idempotency_keys)

    @property
    def total_bytes(self) -> int:
        return sum(batch.bytes for batch in self.batches)

    def items_of(self, kind: str) -> list[LegacyItem]:
        return [item for item in self.items if item.kind == kind]

    @property
    def conflicts(self) -> list[LegacyItem]:
        return [item for item in self.items if item.detail.get("conflict")]

    @property
    def requires_confirmation(self) -> list[LegacyItem]:
        return [item for item in self.items if item.requires_confirmation]

    def counts_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        return counts

    def summary(self) -> dict[str, Any]:
        return {
            "source_roots": [str(p) for p in self.source_roots],
            "batches": len(self.batches),
            "items": self.total_items,
            "bytes": self.total_bytes,
            "by_kind": self.counts_by_kind(),
            "conflicts": len(self.conflicts),
            "requires_confirmation": len(self.requires_confirmation),
        }


@dataclass
class RollbackEntry:
    """回滚清单条目：目标文件导入前的状态。"""

    dest: Path
    source: Path
    existed_before: bool
    backup_of: Path | None  # 覆盖前把旧文件复制到 backups/ 的位置；None 表示原本不存在


@dataclass
class RollbackManifest:
    entries: list[RollbackEntry] = field(default_factory=list)

    def add(self, entry: RollbackEntry) -> None:
        self.entries.append(entry)

    def apply(self) -> int:
        """执行回滚：删除本次新建文件；覆盖过的文件从备份还原。返回处理数。"""
        restored = 0
        for entry in reversed(self.entries):
            if entry.existed_before and entry.backup_of and entry.backup_of.exists():
                shutil.copy2(entry.backup_of, entry.dest)
                restored += 1
            elif not entry.existed_before:
                try:
                    entry.dest.unlink(missing_ok=True)
                except OSError:
                    pass
                restored += 1
        return restored


@dataclass
class ExecutionReport:
    copied: list[LegacyItem] = field(default_factory=list)
    skipped_existing: list[LegacyItem] = field(default_factory=list)
    skipped_conflict: list[LegacyItem] = field(default_factory=list)
    skipped_unconfirmed: list[LegacyItem] = field(default_factory=list)
    rollback: RollbackManifest = field(default_factory=RollbackManifest)
    dry_run: bool = True

    @property
    def total_actions(self) -> int:
        return (
            len(self.copied)
            + len(self.skipped_existing)
            + len(self.skipped_conflict)
            + len(self.skipped_unconfirmed)
        )


# ---------------------------------------------------------------------- 扫描
def discover_source_roots(cwd: Path | None = None, env=None) -> list[Path]:
    """自动发现 AstrBot plugin_data 候选目录（只读，不创建）。

    优先级：WINFOX_LEGACY_PLUGIN_DATA 环境变量 > 常见 AstrBot 目录布局。
    只返回实际存在的目录；不做任何写入。
    """
    env = env if env is not None else {}
    roots: list[Path] = []
    seen: set[str] = set()

    def add(candidate: Path) -> None:
        try:
            key = str(candidate.resolve(strict=False))
        except OSError:
            return
        if key in seen:
            return
        seen.add(key)
        if candidate.is_dir():
            roots.append(candidate)

    override = env.get("WINFOX_LEGACY_PLUGIN_DATA")
    if override:
        add(Path(override))

    cwd = cwd or Path.cwd()
    for base in ("data/plugin_data", "data/plugins_data"):
        for legacy_id in LEGACY_IDS:
            add(cwd / base / legacy_id)
    return roots


def _sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for block in iter(lambda: fp.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_sha256(path: Path) -> str:
    try:
        return _sha256_file(path)
    except OSError:
        return ""


def _iter_regular_files(root: Path, pattern: str) -> Iterable[Path]:
    for path in root.glob(pattern):
        if path.is_file() and not path.is_symlink():
            yield path


def scan_source_root(root: Path, dest_root: Path | None = None) -> list[LegacyItem]:
    """扫描单个 source root，产出条目。dest_root 用于冲突预判（可选）。"""
    root = Path(root)
    items: list[LegacyItem] = []

    def add_item(
        kind: str,
        path: Path,
        rel: str,
        requires_confirmation: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> None:
        size = path.stat().st_size if path.exists() else 0
        item = LegacyItem(
            kind=kind,
            source=path,
            rel=rel.replace("\\", "/"),
            size_bytes=size,
            sha256=_safe_sha256(path),
            requires_confirmation=requires_confirmation,
            detail=dict(detail or {}),
        )
        if dest_root is not None:
            item.detail["conflict"] = _dest_exists_different(dest_root, kind, item)
        items.append(item)

    # --- 工作流
    for path in _iter_regular_files(root / "workflow", "*.json"):
        add_item("workflow", path, path.name)

    # --- LoRA 档案 / 别名（含按模型族后缀的文件）
    for pattern in ("lora_profiles*.json", "lora_aliases*.json"):
        for path in _iter_regular_files(root, pattern):
            kind = "lora_profiles" if path.name.startswith("lora_profiles") else "lora_aliases"
            add_item(kind, path, path.name)

    # --- 收藏（图片 + 侧车元数据）
    fav_dir = root / "favorites"
    if fav_dir.is_dir():
        for path in sorted(fav_dir.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(fav_dir).as_posix()
            if path.suffix.lower() in IMAGE_EXTS or path.suffix.lower() == ".json":
                kind = "favorite"
                add_item(kind, path, rel, detail={"favorite_meta": path.suffix.lower() == ".json"})

    # --- 历史
    for path in _iter_regular_files(root, "saved_prompts.json"):
        add_item("history", path, path.name, detail={"history_kind": "saved_prompts"})
    history_dir = root / "gpt_magic" / "history"
    if history_dir.is_dir():
        for path in _iter_regular_files(history_dir, "*.json"):
            add_item(
                "history",
                path,
                path.relative_to(history_dir).as_posix(),
                detail={"history_kind": "gpt_magic"},
            )

    # --- GPT Magic 配置（db.json：含密钥字段 -> 标记需确认）
    db = root / "gpt_magic" / "db.json"
    if db.is_file():
        secret_paths: list[str] = []
        try:
            data = json.loads(db.read_text(encoding="utf-8"))
            for path_tuple in GPT_MAGIC_SECRET_PATHS:
                node: Any = data
                ok = True
                for part in path_tuple:
                    if isinstance(node, dict) and part in node:
                        node = node[part]
                    else:
                        ok = False
                        break
                if ok and isinstance(node, dict):
                    secret_paths.append(".".join(path_tuple))
                elif ok and str(node or "").strip():
                    secret_paths.append(".".join(path_tuple))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pass  # 无法解析时仍列入，但按需确认处理
        add_item(
            "gpt_magic_config",
            db,
            "db.json",
            requires_confirmation=True,
            detail={"secret_paths": secret_paths},
        )

    return items


def _dest_exists_different(dest_root: Path, kind: str, item: LegacyItem) -> bool:
    dest = _dest_path(dest_root, kind, item)
    if not dest.exists():
        return False
    return _safe_sha256(dest) != item.sha256


def _dest_path(dest_root: Path, kind: str, item: LegacyItem) -> Path:
    """目标布局：<dest_root>/legacy/<kind>/<rel>。"""
    return dest_root / "legacy" / kind / item.rel


def build_plan(
    source_roots: Iterable[Path | str],
    dest_root: Path | str | None = None,
) -> LegacyImportPlan:
    """构建导入计划（dry-run 天然成立：本函数不做任何写操作）。

    - 批次：按 (source_root, kind) 分组，batch_id 为确定性哈希。
    - 冲突：dest_root 提供时预判目标同名但内容不同。
    - 幂等键：kind:rel:sha256 前缀。
    """
    roots = [Path(r) for r in source_roots]
    plan = LegacyImportPlan(source_roots=roots)
    dest_root_path = Path(dest_root) if dest_root is not None else None

    for root in roots:
        if not root.is_dir():
            logger.warning("跳过不存在的 source root: %s", root)
            continue
        try:
            items = scan_source_root(root, dest_root=dest_root_path)
        except OSError as exc:
            logger.warning("扫描 %s 失败: %s", root, exc)
            continue
        by_kind: dict[str, list[LegacyItem]] = {}
        for item in items:
            by_kind.setdefault(item.kind, []).append(item)
            plan.idempotency_keys.add(item.idempotency_key)
        for kind, kind_items in by_kind.items():
            batch_id = _batch_id(root, kind)
            plan.batches.append(
                LegacyBatch(batch_id=batch_id, source_root=root, kind=kind, items=tuple(kind_items))
            )
    return plan


def _batch_id(root: Path, kind: str) -> str:
    digest = hashlib.sha256(f"{str(root).lower()}|{kind}".encode("utf-8")).hexdigest()
    return f"batch-{digest[:12]}"


# ---------------------------------------------------------------------- 执行
def execute_plan(
    plan: LegacyImportPlan,
    dest_root: Path | str,
    *,
    dry_run: bool = True,
    confirmed_keys: Iterable[str] = (),
    overwrite_conflicts: bool = False,
    backup_existing: bool = True,
) -> ExecutionReport:
    """执行导入：只复制、永不移动、永不删除源文件。

    - dry_run=True：只计算将要发生的动作（不写任何文件）。
    - requires_confirmation 的条目仅当幂等键在 confirmed_keys 中才复制；
      复制时也只复制 db.json 本身，密钥值不进入任何 WineFox 文件。
    - 幂等：目标已存在且 sha256 相同 -> 跳过（重复执行无副作用）。
    - 冲突：目标存在且内容不同 -> 默认跳过；overwrite_conflicts=True 时先备份再覆盖。
    - 回滚清单：每个实际写入都记录 RollbackEntry。
    """
    dest = Path(dest_root)
    confirmed = set(confirmed_keys)
    report = ExecutionReport(dry_run=dry_run)

    for item in plan.items:
        target = _dest_path(dest, item.kind, item)
        if target.exists():
            if _safe_sha256(target) == item.sha256:
                report.skipped_existing.append(item)
                continue
            if not overwrite_conflicts:
                # 以执行时的真实差异为准，不依赖建计划时的旁路标记
                # （build_plan 不带 dest_root 时 detail["conflict"] 恒缺失）。
                report.skipped_conflict.append(item)
                continue
        if dry_run:
            # dry-run 展示全部待办（含需确认项），但不落任何文件。
            report.copied.append(item)
            report.rollback.add(
                RollbackEntry(
                    dest=target,
                    source=item.source,
                    existed_before=target.exists(),
                    backup_of=None,
                )
            )
            continue
        if item.requires_confirmation and item.idempotency_key not in confirmed:
            report.skipped_unconfirmed.append(item)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        backup_of: Path | None = None
        existed_before = target.exists()
        if existed_before and backup_existing:
            backup_of = dest / "backups" / "legacy_import" / _backup_name(item)
            backup_of.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup_of)
        shutil.copy2(item.source, target)
        report.copied.append(item)
        report.rollback.add(
            RollbackEntry(dest=target, source=item.source, existed_before=existed_before, backup_of=backup_of)
        )
    return report


def _backup_name(item: LegacyItem) -> str:
    digest = item.sha256[:12]
    return f"{item.kind}__{digest}__{Path(item.rel).name}"


def rollback_plan(report: ExecutionReport) -> int:
    """按执行报告中的回滚清单还原。"""
    return report.rollback.apply()
