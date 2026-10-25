# -*- coding: utf-8 -*-
"""WineFox Desktop 持久化层（仅标准库 sqlite3，无 ORM）。

职责：
- :class:`Database`：数据库连接管理、PRAGMA（WAL / foreign_keys=ON /
  busy_timeout=5000）、显式事务、迁移 runner（幂等、失败完整回滚并保留原库）。
- :class:`JobStore`：任务创建（幂等）/状态机更新/重启恢复。
- :class:`HistoryStore`：历史写入、游标分页、软删除、恢复（只存相对路径）。
- :class:`FavoriteStore`：收藏（scope+ref_key 唯一）。
- :class:`LoraProfileStore`：LoRA 档案（model_family+normalized_key 复合主键）。
- :class:`WorkflowRegistry`：工作流注册表。
- :class:`AppEventStore`：应用事件日志。

并发模型：WAL 支持多读者 + 单写者；写事务使用 ``BEGIN IMMEDIATE`` 提前抢占
写锁，busy_timeout=5000 毫秒内等待，超时抛 sqlite3.OperationalError。
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from .storage_models import (
    AppEventRecord,
    CorruptDatabaseError,
    DOWNLOAD_ACTIVE_STATES,
    DOWNLOAD_TERMINAL_STATES,
    FavoriteRecord,
    HistoryRecord,
    InvalidStateTransitionError,
    JobRecord,
    JobState,
    LoraAssetRecord,
    LoraAssetState,
    LoraDownloadJobRecord,
    LoraDownloadState,
    LoraProfileRecord,
    MigrationError,
    ValidationError,
    WorkflowRecord,
    assert_download_transition,
    assert_transition,
    json_dumps,
    json_loads,
    normalize_relative_path,
    utcnow_iso,
    validate_json,
    validate_public_json,
)

logger = logging.getLogger(__name__)

# migrations 目录固定位于 winefox_core 的上一级
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_BUSY_TIMEOUT_MS = 5000
_MIGRATION_FILE_RE = re.compile(r"^\d{4}_.+\.py$")

# ---------------------------------------------------------------------------
# Agent 02 领域类型（并行开发中可能缺失）：缺失时降级为本地占位类型，
# 保证本模块不依赖尚未合入的类型。
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 取决于 Agent 02 是否已合入
    from winefox_core._agent_types import AgentJobType  # type: ignore
    _AGENT_TYPES_AVAILABLE = True
except Exception:  # pragma: no cover - 缺失时的降级路径
    from typing import Dict as _Dict, Any as _Any
    AgentJobType = _Dict[str, _Any]
    _AGENT_TYPES_AVAILABLE = False

__all__ = [
    "Database",
    "JobStore",
    "HistoryStore",
    "FavoriteStore",
    "LoraProfileStore",
    "LoraAssetStore",
    "LoraDownloadJobStore",
    "WorkflowRegistry",
    "AppEventStore",
    "run_migrations",
    "MIGRATIONS_DIR",
    "_AGENT_TYPES_AVAILABLE",
    "AgentJobType",
]


# ---------------------------------------------------------------------------
# Database：连接 / PRAGMA / 事务 / 迁移
# ---------------------------------------------------------------------------

class Database:
    """SQLite 数据库封装。

    :param path: 数据库文件路径（外部注入，不内置默认路径）。
    :param migrations_dir: 迁移目录，默认 ``<源码根>/migrations``。
    """

    def __init__(self, path: Union[str, Path], migrations_dir: Optional[Path] = None):
        self.path = Path(path)
        self.migrations_dir = Path(migrations_dir) if migrations_dir else MIGRATIONS_DIR
        self._conn: Optional[sqlite3.Connection] = None
        self._tx_depth = 0

    # -- 连接 ---------------------------------------------------------------

    def _create_connection(self) -> sqlite3.Connection:
        """建立连接并应用 PRAGMA。损坏文件在此抛出 CorruptDatabaseError。"""
        conn = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=%d" % _BUSY_TIMEOUT_MS)
            conn.execute("PRAGMA synchronous=NORMAL")
            return conn
        except sqlite3.DatabaseError as exc:
            # 关闭半初始化连接，避免 Windows 上残留文件锁
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            raise CorruptDatabaseError(
                f"无法打开数据库 {self.path}: {exc}"
            ) from exc

    def open(self) -> "Database":
        """打开（或重新打开）主连接。"""
        self.close()
        self._conn = self._create_connection()
        return self

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
        self._tx_depth = 0

    def __del__(self):
        """防御性关闭：对象被回收时确保底层连接释放（避免 Windows 文件锁）。"""
        try:
            self.close()
        except Exception:  # pragma: no cover - 回收路径不容许异常
            pass

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self.open()
        return self._conn  # type: ignore[return-value]

    def new_connection(self) -> sqlite3.Connection:
        """为其他线程/调用方建立独立连接（WAL 多读者）。"""
        return self._create_connection()

    def __enter__(self) -> "Database":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- PRAGMA 状态 --------------------------------------------------------

    def pragma(self, name: str) -> Any:
        """读取 PRAGMA 值，便于测试与诊断。"""
        row = self.connection.execute(f"PRAGMA {name}").fetchone()
        return row[0] if row else None

    def verify_integrity(self) -> List[str]:
        """PRAGMA quick_check；返回非 ok 的条目列表。"""
        rows = self.connection.execute("PRAGMA quick_check").fetchall()
        bad = [str(r[0]) for r in rows if not str(r[0]).startswith("ok")]
        return bad

    # -- 事务 ---------------------------------------------------------------

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """显式事务；可重入（嵌套时只由最外层提交/回滚）。

        写事务默认 ``BEGIN IMMEDIATE``（WAL 单写者语义，尽早抢占写锁），
        失败时完整 ROLLBACK。
        """
        conn = self.connection
        if self._tx_depth == 0:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        self._tx_depth += 1
        try:
            yield conn
        except BaseException:
            if self._tx_depth == 1:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        else:
            if self._tx_depth == 1:
                conn.execute("COMMIT")
        finally:
            self._tx_depth = max(0, self._tx_depth - 1)

    # -- 迁移 ---------------------------------------------------------------

    def initialize(self) -> List[str]:
        """打开数据库并运行全部未应用的迁移。幂等。"""
        self.open()
        return run_migrations(self, migrations_dir=self.migrations_dir)

    def list_applied_migrations(self) -> List[str]:
        conn = self.connection
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        return [
            str(r[0])
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]


def _load_migration_module(path: Path):
    """按文件路径加载迁移模块（不依赖包导入）。"""
    stem = path.stem
    spec = importlib.util.spec_from_file_location(f"winefox_migrations.{stem}", str(path))
    if spec is None or spec.loader is None:  # pragma: no cover - 防御
        raise MigrationError(f"无法加载迁移文件: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "upgrade"):
        raise MigrationError(f"迁移文件缺少 upgrade(conn): {path.name}")
    return module


def _discover_migrations(migrations_dir: Path) -> List[Tuple[str, Path]]:
    if not migrations_dir.is_dir():
        raise MigrationError(f"迁移目录不存在: {migrations_dir}")
    found = []
    for path in sorted(migrations_dir.iterdir()):
        if path.is_file() and _MIGRATION_FILE_RE.match(path.name):
            found.append((path.stem, path))
    return found


def _snapshot(db: Database) -> Path:
    """用 SQLite backup API 生成一致快照（包含 WAL 内容）。"""
    fd, tmp_path = tempfile.mkstemp(
        prefix="winefox_migrate_", suffix=".snapshot.db", dir=str(db.path.parent)
    )
    os.close(fd)
    dest = sqlite3.connect(tmp_path)
    try:
        db.connection.backup(dest)
    finally:
        dest.close()
    return Path(tmp_path)


def _restore_snapshot(db: Database, snapshot: Path) -> None:
    """迁移失败后恢复原库：关连接 -> 覆盖主文件 -> 清理 WAL/SHM。

    恢复后保持关闭状态，由调用方决定何时重新 open。
    """
    db.close()
    for suffix in ("-wal", "-shm"):
        leftover = Path(str(db.path) + suffix)
        if leftover.exists():
            leftover.unlink(missing_ok=True)
    shutil.copyfile(snapshot, db.path)


def run_migrations(
    db: Database, migrations_dir: Optional[Path] = None
) -> List[str]:
    """运行迁移。幂等：已应用的版本直接跳过。

    失败语义：任何迁移抛错 -> 整个迁移运行回滚（当前迁移事务 ROLLBACK +
    用迁移前快照恢复数据库文件），原数据库完整保留，然后抛
    :class:`MigrationError`。
    """
    mdir = Path(migrations_dir) if migrations_dir else db.migrations_dir
    snapshot: Optional[Path] = None
    try:
        conn = db.connection
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {str(r[0]) for r in conn.execute(
            "SELECT version FROM schema_migrations").fetchall()}

        pending = [item for item in _discover_migrations(mdir) if item[0] not in applied]
        if not pending:
            return db.list_applied_migrations()

        # 先做快照；任何失败都可整体还原
        snapshot = _snapshot(db)
        applied_ids: List[str] = []
        for migration_id, path in pending:
            module = _load_migration_module(path)
            with db.transaction():
                module.upgrade(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (migration_id, utcnow_iso()),
                )
            applied_ids.append(migration_id)
            logger.info("[WineFoxStorage] 已应用迁移 %s", migration_id)
        return db.list_applied_migrations()
    except CorruptDatabaseError:
        raise
    except Exception as exc:
        db.close()
        if snapshot is not None and snapshot.exists():
            try:
                _restore_snapshot(db, snapshot)
            except Exception as restore_exc:  # pragma: no cover - 极端情况
                raise MigrationError(
                    f"迁移失败且快照恢复失败: {exc}; 恢复错误: {restore_exc}"
                ) from exc
        raise MigrationError(f"数据库迁移失败（已回滚，原库保留）: {exc}") from exc
    finally:
        if snapshot is not None and snapshot.exists():
            try:
                snapshot.unlink()
            except OSError:  # pragma: no cover
                pass


# ---------------------------------------------------------------------------
# 行 -> 记录
# ---------------------------------------------------------------------------

def _row_to_job(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        id=row["id"],
        client_request_id=row["client_request_id"],
        mode=row["mode"],
        state=row["state"],
        request_json=json_loads(row["request_json"], {}),
        comfy_prompt_id=row["comfy_prompt_id"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _row_to_history(row: sqlite3.Row) -> HistoryRecord:
    return HistoryRecord(
        id=row["id"],
        job_id=row["job_id"],
        image_relpath=row["image_relpath"],
        sha256=row["sha256"],
        raw_prompt=row["raw_prompt"] or "",
        final_prompt=row["final_prompt"] or "",
        seed=row["seed"],
        width=row["width"],
        height=row["height"],
        metadata=json_loads(row["metadata"], {}),
        deleted_at=row["deleted_at"],
        created_at=row["created_at"],
    )


def _row_to_favorite(row: sqlite3.Row) -> FavoriteRecord:
    return FavoriteRecord(
        id=row["id"],
        scope=row["scope"],
        ref_key=row["ref_key"],
        kind=row["kind"],
        payload=json_loads(row["payload"], {}),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_lora_profile(row: sqlite3.Row) -> LoraProfileRecord:
    return LoraProfileRecord(
        model_family=row["model_family"],
        normalized_key=row["normalized_key"],
        display_key=row["display_key"],
        profile=json_loads(row["profile"], {}),
        updated_at=row["updated_at"],
        revision=int(row["revision"] or 0),
    )


def _row_to_lora_asset(row: sqlite3.Row) -> LoraAssetRecord:
    return LoraAssetRecord(
        id=row["id"],
        model_family=row["model_family"],
        filename=row["filename"],
        comfy_key=row["comfy_key"] or "",
        display_name=row["display_name"] or "",
        source=row["source"] or "local",
        model_id=row["model_id"],
        version_id=row["version_id"],
        preview_key=row["preview_key"],
        state=row["state"] or "installed",
        quarantine_relpath=row["quarantine_relpath"],
        disabled=bool(row["disabled"]),
        disable_reason=row["disable_reason"],
        info=json_loads(row["info"], {}),
        installed_at=row["installed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_lora_download_job(row: sqlite3.Row) -> LoraDownloadJobRecord:
    return LoraDownloadJobRecord(
        result_id=row["result_id"],
        state=row["state"] or "queued",
        family=row["family"] or "illustrious",
        alias=row["alias"] or "",
        source=row["source"] or "civitai",
        source_ref=json_loads(row["source_ref"], {}),
        progress=json_loads(row["progress"], {}),
        result=json_loads(row["result"], {}),
        part_files=json_loads(row["part_files"], []),
        error=row["error"],
        request_timeout=int(row["request_timeout"] or 60),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _row_to_workflow(row: sqlite3.Row) -> WorkflowRecord:
    return WorkflowRecord(
        id=row["id"],
        name=row["name"],
        version=row["version"],
        kind=row["kind"],
        definition=json_loads(row["definition"], {}),
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_app_event(row: sqlite3.Row) -> AppEventRecord:
    return AppEventRecord(
        id=row["id"],
        event_type=row["event_type"],
        payload=json_loads(row["payload"], {}),
        created_at=row["created_at"],
    )


def _fetch_one(conn: sqlite3.Connection, sql: str, params: tuple) -> Optional[sqlite3.Row]:
    return conn.execute(sql, params).fetchone()


# ---------------------------------------------------------------------------
# JobStore
# ---------------------------------------------------------------------------

class JobStore:
    """任务表。client_request_id 唯一，创建幂等，状态迁移受状态机约束。"""

    def __init__(self, db: Database):
        self.db = db

    # -- 创建（幂等） -------------------------------------------------------

    def create(
        self,
        client_request_id: str,
        mode: str,
        request_json: Dict[str, Any],
        state: JobState = JobState.PENDING,
    ) -> JobRecord:
        """创建任务。client_request_id 已存在时直接返回既有记录（幂等）。"""
        if not str(client_request_id or "").strip():
            raise InvalidStateTransitionError("client_request_id 不能为空")
        if not str(mode or "").strip():
            raise InvalidStateTransitionError("mode 不能为空")
        validate_json(request_json, name="request_json")
        now = utcnow_iso()
        conn = self.db.connection
        with self.db.transaction():
            cur = conn.execute(
                "INSERT OR IGNORE INTO jobs (client_request_id, mode, state,"
                " request_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(client_request_id).strip(),
                    str(mode).strip(),
                    JobState(state).value,
                    json_dumps(request_json),
                    now,
                    now,
                ),
            )
            if cur.rowcount == 0:
                # 已存在：幂等返回
                row = _fetch_one(
                    conn,
                    "SELECT * FROM jobs WHERE client_request_id = ?",
                    (str(client_request_id).strip(),),
                )
                return _row_to_job(row)  # type: ignore[arg-type]
            return self.get(cur.lastrowid)  # type: ignore[arg-type]

    # -- 查询 ---------------------------------------------------------------

    def get(self, job_id: int) -> Optional[JobRecord]:
        row = _fetch_one(
            self.db.connection, "SELECT * FROM jobs WHERE id = ?", (int(job_id),)
        )
        return _row_to_job(row) if row else None

    def get_by_client_request_id(self, client_request_id: str) -> Optional[JobRecord]:
        row = _fetch_one(
            self.db.connection,
            "SELECT * FROM jobs WHERE client_request_id = ?",
            (str(client_request_id).strip(),),
        )
        return _row_to_job(row) if row else None

    def list(
        self,
        state: Optional[JobState] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[JobRecord]:
        sql = "SELECT * FROM jobs"
        params: list = []
        if state is not None:
            sql += " WHERE state = ?"
            params.append(JobState(state).value)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        rows = self.db.connection.execute(sql, params).fetchall()
        return [_row_to_job(r) for r in rows]

    # -- 状态更新（状态机校验） ---------------------------------------------

    def update_state(
        self,
        job_id: int,
        new_state: JobState,
        *,
        error: Optional[str] = None,
        comfy_prompt_id: Optional[str] = None,
    ) -> JobRecord:
        """更新状态。非法迁移抛出 :class:`InvalidStateTransitionError`，
        且不产生任何写入（事务回滚）。"""
        new_state = JobState(new_state)
        conn = self.db.connection
        with self.db.transaction():
            row = _fetch_one(
                conn, "SELECT * FROM jobs WHERE id = ?", (int(job_id),)
            )
            if row is None:
                raise InvalidStateTransitionError(f"任务不存在: {job_id}")
            assert_transition(JobState(row["state"]), new_state)
            now = utcnow_iso()
            set_parts = ["state = ?", "updated_at = ?"]
            params: list = [new_state.value, now]
            if error is not None:
                set_parts.append("error = ?")
                params.append(str(error))
            if comfy_prompt_id is not None:
                set_parts.append("comfy_prompt_id = ?")
                params.append(str(comfy_prompt_id))
            if new_state is JobState.RUNNING:
                set_parts.append("started_at = COALESCE(started_at, ?)")
                params.append(now)
            if new_state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
                set_parts.append("finished_at = ?")
                params.append(now)
            params.append(int(job_id))
            conn.execute(
                f"UPDATE jobs SET {', '.join(set_parts)} WHERE id = ?", params
            )
            updated = _fetch_one(conn, "SELECT * FROM jobs WHERE id = ?", (int(job_id),))
            return _row_to_job(updated)  # type: ignore[arg-type]

    # -- 重启恢复 -----------------------------------------------------------

    def recover_interrupted(
        self,
        *,
        stale_before: Optional[str] = None,
        force: bool = False,
    ) -> List[JobRecord]:
        """应用重启后恢复：把卡在非终止状态（pending/running）的任务
        重置为 pending，允许重新派发。

        :param stale_before: 只恢复 updated_at 早于该时间的任务（默认全部）。
        :param force: 为 True 时连 pending 也一并恢复（默认仅 running）。
        """
        conn = self.db.connection
        recovered: List[JobRecord] = []
        with self.db.transaction():
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state = 'running'"
            ).fetchall()
            for row in rows:
                if stale_before and row["updated_at"] >= stale_before:
                    continue
                assert_transition(JobState(row["state"]), JobState.PENDING)
                now = utcnow_iso()
                conn.execute(
                    "UPDATE jobs SET state = 'pending', updated_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                recovered.append(_row_to_job(_fetch_one(conn, "SELECT * FROM jobs WHERE id = ?", (row["id"],))))  # type: ignore[arg-type]
            if force:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE state = 'pending'"
                ).fetchall()
                for row in rows:
                    recovered.append(_row_to_job(row))
        if recovered:
            try:
                AppEventStore(self.db).record(
                    "job.recovered",
                    {"count": len(recovered), "ids": [r.id for r in recovered]},
                )
            except Exception:  # pragma: no cover - 事件写入失败不应阻断恢复
                logger.warning("[WineFoxStorage] 恢复事件写入失败", exc_info=True)
        return recovered


# ---------------------------------------------------------------------------
# HistoryStore
# ---------------------------------------------------------------------------

class HistoryStore:
    """生成历史。job_id 唯一；只存相对路径；软删除 + 恢复；游标分页。"""

    def __init__(self, db: Database):
        self.db = db

    def add(
        self,
        job_id: int,
        image_relpath: str,
        sha256: Optional[str] = None,
        raw_prompt: str = "",
        final_prompt: str = "",
        seed: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> HistoryRecord:
        """写入一条历史。job_id 已存在时更新该行（幂等）。
        image_relpath 必须是相对路径，sha256 必须是 64 位十六进制。"""
        relpath = normalize_relative_path(image_relpath, name="image_relpath")
        if sha256 is not None:
            sha = str(sha256).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ValidationError(f"sha256 必须是 64 位十六进制: {sha!r}")
            sha256 = sha
        meta = dict(metadata or {})
        validate_json(meta, name="metadata")
        now = utcnow_iso()
        conn = self.db.connection
        with self.db.transaction():
            conn.execute(
                "INSERT INTO history (job_id, image_relpath, sha256, raw_prompt,"
                " final_prompt, seed, width, height, metadata, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(job_id) DO UPDATE SET"
                " image_relpath=excluded.image_relpath, sha256=excluded.sha256,"
                " raw_prompt=excluded.raw_prompt, final_prompt=excluded.final_prompt,"
                " seed=excluded.seed, width=excluded.width, height=excluded.height,"
                " metadata=excluded.metadata",
                (
                    int(job_id), relpath, sha256,
                    str(raw_prompt or ""), str(final_prompt or ""),
                    seed, width, height,
                    json_dumps(meta), now,
                ),
            )
            row = _fetch_one(conn, "SELECT * FROM history WHERE job_id = ?", (int(job_id),))
            return _row_to_history(row)  # type: ignore[arg-type]

    # -- 查询 / 游标分页 -----------------------------------------------------

    def get_by_job_id(self, job_id: int) -> Optional[HistoryRecord]:
        row = _fetch_one(
            self.db.connection, "SELECT * FROM history WHERE job_id = ?", (int(job_id),)
        )
        return _row_to_history(row) if row else None

    def list_cursor(
        self,
        after_id: Optional[int] = None,
        limit: int = 50,
        include_deleted: bool = False,
    ) -> Tuple[List[HistoryRecord], Optional[int]]:
        """按 id 倒序游标分页。

        :param after_id: 只返回 id 小于该值的行（上一页最后一条 id）。
        :param limit: 每页条数（1..500）。
        :param include_deleted: 是否包含软删除行。
        :return: (records, next_cursor)；next_cursor 为 None 表示没有更多。
        """
        limit = max(1, min(500, int(limit)))
        sql = "SELECT * FROM history"
        where = []
        params: list = []
        if after_id is not None:
            where.append("id < ?")
            params.append(int(after_id))
        if not include_deleted:
            where.append("deleted_at IS NULL")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit + 1)  # 多取一条判断是否还有下一页
        rows = self.db.connection.execute(sql, params).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        records = [_row_to_history(r) for r in page]
        next_cursor = page[-1]["id"] if has_more else None
        return records, next_cursor

    # -- 软删除 / 恢复 ------------------------------------------------------

    def soft_delete(self, job_id: int) -> bool:
        """软删除；返回是否命中。已删除的重复调用幂等返回 True。"""
        conn = self.db.connection
        with self.db.transaction():
            cur = conn.execute(
                "UPDATE history SET deleted_at = ? WHERE job_id = ? AND deleted_at IS NULL",
                (utcnow_iso(), int(job_id)),
            )
            return cur.rowcount > 0

    def restore(self, job_id: int) -> bool:
        """恢复软删除；返回是否命中（即确实有删除行被恢复）。"""
        conn = self.db.connection
        with self.db.transaction():
            cur = conn.execute(
                "UPDATE history SET deleted_at = NULL WHERE job_id = ? AND deleted_at IS NOT NULL",
                (int(job_id),),
            )
            return cur.rowcount > 0

    def hard_delete(self, job_id: int) -> bool:
        """物理删除（用于清理）。"""
        conn = self.db.connection
        with self.db.transaction():
            cur = conn.execute(
                "DELETE FROM history WHERE job_id = ?", (int(job_id),)
            )
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# FavoriteStore
# ---------------------------------------------------------------------------

class FavoriteStore:
    """收藏。(scope, ref_key) 唯一；add 幂等，upsert 覆盖更新。"""

    def __init__(self, db: Database):
        self.db = db

    def add(
        self,
        scope: str,
        ref_key: str,
        kind: str = "generic",
        payload: Optional[Dict[str, Any]] = None,
    ) -> FavoriteRecord:
        """添加收藏。已存在时原样返回既有记录（幂等，不覆盖）。"""
        scope = str(scope or "").strip()
        ref_key = str(ref_key or "").strip()
        if not scope or not ref_key:
            raise ValidationError("scope/ref_key 不能为空")
        data = dict(payload or {})
        validate_json(data, name="payload")
        now = utcnow_iso()
        conn = self.db.connection
        with self.db.transaction():
            conn.execute(
                "INSERT OR IGNORE INTO favorites (scope, ref_key, kind, payload,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (scope, ref_key, str(kind or "generic"), json_dumps(data), now, now),
            )
            row = _fetch_one(
                conn,
                "SELECT * FROM favorites WHERE scope = ? AND ref_key = ?",
                (scope, ref_key),
            )
            return _row_to_favorite(row)  # type: ignore[arg-type]

    def upsert(
        self,
        scope: str,
        ref_key: str,
        kind: str = "generic",
        payload: Optional[Dict[str, Any]] = None,
    ) -> FavoriteRecord:
        """添加或覆盖收藏（更新 kind/payload/updated_at）。"""
        scope = str(scope or "").strip()
        ref_key = str(ref_key or "").strip()
        if not scope or not ref_key:
            raise ValidationError("scope/ref_key 不能为空")
        data = dict(payload or {})
        validate_json(data, name="payload")
        now = utcnow_iso()
        conn = self.db.connection
        with self.db.transaction():
            conn.execute(
                "INSERT INTO favorites (scope, ref_key, kind, payload, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(scope, ref_key) DO UPDATE SET"
                " kind=excluded.kind, payload=excluded.payload, updated_at=excluded.updated_at",
                (scope, ref_key, str(kind or "generic"), json_dumps(data), now, now),
            )
            row = _fetch_one(
                conn,
                "SELECT * FROM favorites WHERE scope = ? AND ref_key = ?",
                (scope, ref_key),
            )
            return _row_to_favorite(row)  # type: ignore[arg-type]

    def get(self, scope: str, ref_key: str) -> Optional[FavoriteRecord]:
        row = _fetch_one(
            self.db.connection,
            "SELECT * FROM favorites WHERE scope = ? AND ref_key = ?",
            (str(scope or "").strip(), str(ref_key or "").strip()),
        )
        return _row_to_favorite(row) if row else None

    def list(self, scope: Optional[str] = None, kind: Optional[str] = None) -> List[FavoriteRecord]:
        sql = "SELECT * FROM favorites"
        where, params = [], []
        if scope is not None:
            where.append("scope = ?")
            params.append(str(scope))
        if kind is not None:
            where.append("kind = ?")
            params.append(str(kind))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        return [_row_to_favorite(r) for r in self.db.connection.execute(sql, params).fetchall()]

    def remove(self, scope: str, ref_key: str) -> bool:
        conn = self.db.connection
        with self.db.transaction():
            cur = conn.execute(
                "DELETE FROM favorites WHERE scope = ? AND ref_key = ?",
                (str(scope or "").strip(), str(ref_key or "").strip()),
            )
            return cur.rowcount > 0


def _raise_key_error(name: str) -> None:
    raise ValidationError(f"{name} 不能为空")


# ---------------------------------------------------------------------------
# LoraProfileStore
# ---------------------------------------------------------------------------

def normalize_lora_key(value: str) -> str:
    """LoRA 档案键规范化：小写、去首尾空白、空白折叠为单个空格。"""
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


class LoraProfileStore:
    """LoRA 档案。(model_family, normalized_key) 复合主键，upsert 语义。"""

    def __init__(self, db: Database):
        self.db = db

    def save(
        self,
        model_family: str,
        key: str,
        profile: Dict[str, Any],
    ) -> LoraProfileRecord:
        family = str(model_family or "").strip()
        normalized = normalize_lora_key(key)
        if not family or not normalized:
            raise _raise_key_error("model_family/key")
        validate_json(profile, name="profile")
        now = utcnow_iso()
        conn = self.db.connection
        with self.db.transaction():
            conn.execute(
                "INSERT INTO lora_profiles (model_family, normalized_key, display_key,"
                " profile, updated_at, revision) VALUES (?, ?, ?, ?, ?, 1)"
                " ON CONFLICT(model_family, normalized_key) DO UPDATE SET"
                " display_key=excluded.display_key, profile=excluded.profile,"
                " updated_at=excluded.updated_at,"
                " revision=lora_profiles.revision + 1",
                (family, normalized, str(key or "").strip(), json_dumps(profile), now),
            )
            row = _fetch_one(
                conn,
                "SELECT * FROM lora_profiles WHERE model_family = ? AND normalized_key = ?",
                (family, normalized),
            )
            return _row_to_lora_profile(row)  # type: ignore[arg-type]

    def get(self, model_family: str, key: str) -> Optional[LoraProfileRecord]:
        row = _fetch_one(
            self.db.connection,
            "SELECT * FROM lora_profiles WHERE model_family = ? AND normalized_key = ?",
            (str(model_family or "").strip(), normalize_lora_key(key)),
        )
        return _row_to_lora_profile(row) if row else None

    def get_family(self, model_family: str) -> List[LoraProfileRecord]:
        rows = self.db.connection.execute(
            "SELECT * FROM lora_profiles WHERE model_family = ? ORDER BY normalized_key",
            (str(model_family or "").strip(),),
        ).fetchall()
        return [_row_to_lora_profile(r) for r in rows]

    def list_families(self) -> List[str]:
        rows = self.db.connection.execute(
            "SELECT DISTINCT model_family FROM lora_profiles ORDER BY model_family"
        ).fetchall()
        return [str(r[0]) for r in rows]

    def delete(self, model_family: str, key: str) -> bool:
        conn = self.db.connection
        with self.db.transaction():
            cur = conn.execute(
                "DELETE FROM lora_profiles WHERE model_family = ? AND normalized_key = ?",
                (str(model_family or "").strip(), normalize_lora_key(key)),
            )
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# LoraAssetStore
# ---------------------------------------------------------------------------

def _clean_lora_filename(filename: str) -> str:
    """ComfyUI LoRA identity is a safe relative path, including subfolders."""
    return normalize_relative_path(filename, name="filename")


class LoraAssetStore:
    """本地库资产索引。只记录索引与相对路径；权重文件不由此层删除/改写。

    - 删除只删索引（delete）。
    - 隔离（quarantine）只把 state 置为 quarantined 并记录隔离后相对路径，
      恢复（restore）只能回原位（清空隔离路径）。
    - info / preview_key 为公开 JSON，禁止携带 token URL / Cookie / 绝对路径。
    """

    def __init__(self, db: Database):
        self.db = db

    def register(
        self,
        model_family: str,
        filename: str,
        *,
        comfy_key: str = "",
        display_name: str = "",
        source: str = "local",
        model_id: Optional[int] = None,
        version_id: Optional[int] = None,
        preview_key: Optional[str] = None,
        info: Optional[Dict[str, Any]] = None,
        installed_at: Optional[str] = None,
    ) -> LoraAssetRecord:
        family = str(model_family or "").strip().lower()
        if not family:
            raise _raise_key_error("model_family")
        name = _clean_lora_filename(filename)
        data = dict(info or {})
        validate_public_json(data, name="info")
        if preview_key is not None:
            preview_key = normalize_relative_path(preview_key, name="preview_key")
        now = utcnow_iso()
        conn = self.db.connection
        with self.db.transaction():
            conn.execute(
                "INSERT INTO lora_assets (model_family, filename, comfy_key, display_name,"
                " source, model_id, version_id, preview_key, info, installed_at,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(model_family, filename) DO UPDATE SET"
                " comfy_key=excluded.comfy_key, display_name=excluded.display_name,"
                " source=excluded.source, model_id=excluded.model_id,"
                " version_id=excluded.version_id, preview_key=excluded.preview_key,"
                " info=excluded.info, installed_at=excluded.installed_at,"
                " updated_at=excluded.updated_at",
                (
                    family, name, str(comfy_key or "").strip(),
                    str(display_name or "").strip(), str(source or "local").strip(),
                    model_id, version_id, preview_key, json_dumps(data), installed_at,
                    now, now,
                ),
            )
            row = _fetch_one(
                conn,
                "SELECT * FROM lora_assets WHERE model_family = ? AND filename = ?",
                (family, name),
            )
            return _row_to_lora_asset(row)  # type: ignore[arg-type]

    def get(self, model_family: str, filename: str) -> Optional[LoraAssetRecord]:
        row = _fetch_one(
            self.db.connection,
            "SELECT * FROM lora_assets WHERE model_family = ? AND filename = ?",
            (str(model_family or "").strip(), _clean_lora_filename(filename)),
        )
        return _row_to_lora_asset(row) if row else None

    def get_by_id(self, asset_id: int) -> Optional[LoraAssetRecord]:
        row = _fetch_one(
            self.db.connection, "SELECT * FROM lora_assets WHERE id = ?", (int(asset_id),)
        )
        return _row_to_lora_asset(row) if row else None

    def list(
        self,
        model_family: Optional[str] = None,
        state: Optional[str] = None,
        include_disabled: bool = True,
        limit: int = 200,
        offset: int = 0,
    ) -> List[LoraAssetRecord]:
        sql = "SELECT * FROM lora_assets"
        where: list[str] = []
        params: list = []
        if model_family is not None:
            where.append("model_family = ?")
            params.append(str(model_family).strip().lower())
        if state is not None:
            where.append("state = ?")
            params.append(LoraAssetState(state).value)
        if not include_disabled:
            where.append("disabled = 0")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY filename LIMIT ? OFFSET ?"
        params += [max(1, min(2000, int(limit))), max(0, int(offset))]
        return [_row_to_lora_asset(r) for r in self.db.connection.execute(sql, params).fetchall()]

    def filenames(self, model_family: Optional[str] = None, state: Optional[str] = None) -> List[str]:
        """返回可安装文件名列表（供与 ComfyUI 实际 inventory 比对）。"""
        sql = "SELECT filename FROM lora_assets"
        where: list[str] = []
        params: list = []
        if model_family is not None:
            where.append("model_family = ?")
            params.append(str(model_family).strip().lower())
        if state is not None:
            where.append("state = ?")
            params.append(LoraAssetState(state).value)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY filename"
        return [str(r[0]) for r in self.db.connection.execute(sql, params).fetchall()]

    def count(self, model_family: Optional[str] = None, state: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM lora_assets"
        where: list[str] = []
        params: list = []
        if model_family is not None:
            where.append("model_family = ?")
            params.append(str(model_family).strip().lower())
        if state is not None:
            where.append("state = ?")
            params.append(LoraAssetState(state).value)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return int(self.db.connection.execute(sql, params).fetchone()["c"])

    def find_by_model_id(self, model_id: int, model_family: Optional[str] = None) -> List[LoraAssetRecord]:
        sql = "SELECT * FROM lora_assets WHERE model_id = ?"
        params: list = [int(model_id)]
        if model_family is not None:
            sql += " AND model_family = ?"
            params.append(str(model_family).strip().lower())
        sql += " ORDER BY id"
        return [_row_to_lora_asset(r) for r in self.db.connection.execute(sql, params).fetchall()]

    def set_preview(self, asset_id: int, preview_key: Optional[str]) -> bool:
        if preview_key is not None:
            preview_key = normalize_relative_path(preview_key, name="preview_key")
        conn = self.db.connection
        with self.db.transaction():
            cur = conn.execute(
                "UPDATE lora_assets SET preview_key = ?, updated_at = ? WHERE id = ?",
                (preview_key, utcnow_iso(), int(asset_id)),
            )
            return cur.rowcount > 0

    def set_disabled(self, asset_id: int, reason: Optional[str] = None) -> bool:
        conn = self.db.connection
        with self.db.transaction():
            cur = conn.execute(
                "UPDATE lora_assets SET disabled = 1, disable_reason = ?, updated_at = ?"
                " WHERE id = ?",
                (str(reason or "").strip()[:512] or None, utcnow_iso(), int(asset_id)),
            )
            return cur.rowcount > 0

    def clear_disabled(self, asset_id: int) -> bool:
        conn = self.db.connection
        with self.db.transaction():
            cur = conn.execute(
                "UPDATE lora_assets SET disabled = 0, disable_reason = NULL, updated_at = ?"
                " WHERE id = ?",
                (utcnow_iso(), int(asset_id)),
            )
            return cur.rowcount > 0

    def quarantine(self, asset_id: int, quarantine_relpath: str) -> LoraAssetRecord:
        """隔离：installed -> quarantined。隔离路径必须是相对路径。"""
        relpath = normalize_relative_path(quarantine_relpath, name="quarantine_relpath")
        conn = self.db.connection
        with self.db.transaction():
            row = _fetch_one(conn, "SELECT * FROM lora_assets WHERE id = ?", (int(asset_id),))
            if row is None:
                raise InvalidStateTransitionError(f"资产不存在: {asset_id}")
            old = LoraAssetState(row["state"])
            if old is LoraAssetState.QUARANTINED:
                raise InvalidStateTransitionError(
                    f"资产已处于隔离状态: {asset_id}"
                )
            conn.execute(
                "UPDATE lora_assets SET state = 'quarantined', quarantine_relpath = ?,"
                " updated_at = ? WHERE id = ?",
                (relpath, utcnow_iso(), int(asset_id)),
            )
            updated = _fetch_one(conn, "SELECT * FROM lora_assets WHERE id = ?", (int(asset_id),))
            return _row_to_lora_asset(updated)  # type: ignore[arg-type]

    def restore(self, asset_id: int) -> LoraAssetRecord:
        """恢复：quarantined -> installed，仅回原位（清空隔离路径）。"""
        conn = self.db.connection
        with self.db.transaction():
            row = _fetch_one(conn, "SELECT * FROM lora_assets WHERE id = ?", (int(asset_id),))
            if row is None:
                raise InvalidStateTransitionError(f"资产不存在: {asset_id}")
            if LoraAssetState(row["state"]) is not LoraAssetState.QUARANTINED:
                raise InvalidStateTransitionError(
                    f"资产不在隔离状态，无法恢复: {asset_id}"
                )
            conn.execute(
                "UPDATE lora_assets SET state = 'installed', quarantine_relpath = NULL,"
                " updated_at = ? WHERE id = ?",
                (utcnow_iso(), int(asset_id)),
            )
            updated = _fetch_one(conn, "SELECT * FROM lora_assets WHERE id = ?", (int(asset_id),))
            return _row_to_lora_asset(updated)  # type: ignore[arg-type]

    def delete(self, asset_id: int) -> bool:
        """只删除索引，不触碰权重文件。"""
        conn = self.db.connection
        with self.db.transaction():
            cur = conn.execute("DELETE FROM lora_assets WHERE id = ?", (int(asset_id),))
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# LoraDownloadJobStore
# ---------------------------------------------------------------------------

class LoraDownloadJobStore:
    """下载任务持久化。result_id 主键；状态迁移受状态机约束。

    非终态（queued/resolving/downloading/profiling/refreshing）在应用重启时
    通过 :meth:`mark_interrupted` 原子置为 interrupted。source_ref / result /
    progress / part_files 均为公开 JSON（不含 token URL / Cookie / 绝对路径）。
    """

    def __init__(self, db: Database):
        self.db = db

    def enqueue(
        self,
        result_id: str,
        *,
        family: str = "illustrious",
        alias: str = "",
        source: str = "civitai",
        source_ref: Optional[Dict[str, Any]] = None,
        request_timeout: int = 60,
    ) -> LoraDownloadJobRecord:
        """创建任务。result_id 已存在时原样返回既有记录（幂等，不重置状态）。"""
        rid = str(result_id or "").strip()
        if not rid:
            raise InvalidStateTransitionError("result_id 不能为空")
        ref = dict(source_ref or {})
        validate_public_json(ref, name="source_ref")
        now = utcnow_iso()
        conn = self.db.connection
        with self.db.transaction():
            conn.execute(
                "INSERT OR IGNORE INTO lora_download_jobs (result_id, state, family, alias,"
                " source, source_ref, request_timeout, created_at, updated_at)"
                " VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
                (
                    rid, str(family or "illustrious").strip().lower(),
                    str(alias or "").strip(), str(source or "civitai").strip(),
                    json_dumps(ref), max(10, min(180, int(request_timeout or 60))), now, now,
                ),
            )
            row = _fetch_one(conn, "SELECT * FROM lora_download_jobs WHERE result_id = ?", (rid,))
            return _row_to_lora_download_job(row)  # type: ignore[arg-type]

    def get(self, result_id: str) -> Optional[LoraDownloadJobRecord]:
        row = _fetch_one(
            self.db.connection,
            "SELECT * FROM lora_download_jobs WHERE result_id = ?",
            (str(result_id or "").strip(),),
        )
        return _row_to_lora_download_job(row) if row else None

    def list(
        self,
        state: Optional[LoraDownloadState] = None,
        family: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[LoraDownloadJobRecord]:
        sql = "SELECT * FROM lora_download_jobs"
        where: list[str] = []
        params: list = []
        if state is not None:
            where.append("state = ?")
            params.append(LoraDownloadState(state).value)
        if family is not None:
            where.append("family = ?")
            params.append(str(family).strip().lower())
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, result_id LIMIT ? OFFSET ?"
        params += [max(1, min(2000, int(limit))), max(0, int(offset))]
        return [_row_to_lora_download_job(r) for r in self.db.connection.execute(sql, params).fetchall()]

    def active_count(self) -> int:
        marks = ", ".join("?" for _ in DOWNLOAD_ACTIVE_STATES)
        row = self.db.connection.execute(
            f"SELECT COUNT(*) AS c FROM lora_download_jobs WHERE state IN ({marks})",
            [s.value for s in DOWNLOAD_ACTIVE_STATES],
        ).fetchone()
        return int(row["c"])

    def update_state(
        self,
        result_id: str,
        new_state: LoraDownloadState,
        *,
        progress: Optional[Dict[str, Any]] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        part_files: Optional[List[str]] = None,
    ) -> LoraDownloadJobRecord:
        """更新状态。非法迁移抛错且事务回滚（无部分写入）。"""
        new_state = LoraDownloadState(new_state)
        conn = self.db.connection
        with self.db.transaction():
            row = _fetch_one(
                conn, "SELECT * FROM lora_download_jobs WHERE result_id = ?",
                (str(result_id or "").strip(),),
            )
            if row is None:
                raise InvalidStateTransitionError(f"下载任务不存在: {result_id}")
            assert_download_transition(LoraDownloadState(row["state"]), new_state)
            now = utcnow_iso()
            set_parts = ["state = ?", "updated_at = ?"]
            params: list = [new_state.value, now]
            if progress is not None:
                progress = dict(progress)
                validate_public_json(progress, name="progress")
                set_parts.append("progress = ?")
                params.append(json_dumps(progress))
            if result is not None:
                result = dict(result)
                validate_public_json(result, name="result")
                set_parts.append("result = ?")
                params.append(json_dumps(result))
            if error is not None:
                set_parts.append("error = ?")
                params.append(str(error)[:2048])
            if part_files is not None:
                cleaned = [
                    normalize_relative_path(item, name="part_files[]") for item in part_files
                ]
                validate_json(cleaned, name="part_files")
                set_parts.append("part_files = ?")
                params.append(json_dumps(cleaned))
            if new_state in DOWNLOAD_ACTIVE_STATES:
                set_parts.append("started_at = COALESCE(started_at, ?)")
                params.append(now)
            if new_state in DOWNLOAD_TERMINAL_STATES:
                set_parts.append("finished_at = ?")
                params.append(now)
            params.append(str(result_id or "").strip())
            conn.execute(
                f"UPDATE lora_download_jobs SET {', '.join(set_parts)} WHERE result_id = ?",
                params,
            )
            updated = _fetch_one(
                conn, "SELECT * FROM lora_download_jobs WHERE result_id = ?",
                (str(result_id or "").strip(),),
            )
            return _row_to_lora_download_job(updated)  # type: ignore[arg-type]

    def set_progress(
        self,
        result_id: str,
        progress: Optional[Dict[str, Any]] = None,
        *,
        part_files: Optional[List[str]] = None,
    ) -> LoraDownloadJobRecord:
        """下载中更新进度/分片文件（纯字段更新，不改状态）。"""
        rid = str(result_id or "").strip()
        conn = self.db.connection
        with self.db.transaction():
            row = _fetch_one(
                conn, "SELECT * FROM lora_download_jobs WHERE result_id = ?", (rid,)
            )
            if row is None:
                raise InvalidStateTransitionError(f"下载任务不存在: {result_id}")
            set_parts = ["updated_at = ?"]
            params: list = [utcnow_iso()]
            if progress is not None:
                progress = dict(progress)
                validate_public_json(progress, name="progress")
                set_parts.append("progress = ?")
                params.append(json_dumps(progress))
            if part_files is not None:
                cleaned = [
                    normalize_relative_path(item, name="part_files[]")
                    for item in part_files
                ]
                validate_json(cleaned, name="part_files")
                set_parts.append("part_files = ?")
                params.append(json_dumps(cleaned))
            params.append(rid)
            conn.execute(
                f"UPDATE lora_download_jobs SET {', '.join(set_parts)} WHERE result_id = ?",
                params,
            )
            updated = _fetch_one(
                conn, "SELECT * FROM lora_download_jobs WHERE result_id = ?", (rid,)
            )
            return _row_to_lora_download_job(updated)  # type: ignore[arg-type]

    def finish(self, result_id: str, result: Dict[str, Any]) -> LoraDownloadJobRecord:
        """完成下载（profiling/refreshing -> completed）。"""
        return self.update_state(
            result_id, LoraDownloadState.COMPLETED, result=result
        )

    def requeue(self, result_id: str) -> LoraDownloadJobRecord:
        """重试：failed/cancelled/interrupted -> queued。"""
        conn = self.db.connection
        with self.db.transaction():
            row = _fetch_one(
                conn, "SELECT * FROM lora_download_jobs WHERE result_id = ?",
                (str(result_id or "").strip(),),
            )
            if row is None:
                raise InvalidStateTransitionError(f"下载任务不存在: {result_id}")
            assert_download_transition(
                LoraDownloadState(row["state"]), LoraDownloadState.QUEUED
            )
            now = utcnow_iso()
            conn.execute(
                "UPDATE lora_download_jobs SET state = 'queued', error = NULL,"
                " progress = '{}', updated_at = ? WHERE result_id = ?",
                (now, str(result_id or "").strip()),
            )
            updated = _fetch_one(
                conn, "SELECT * FROM lora_download_jobs WHERE result_id = ?",
                (str(result_id or "").strip(),),
            )
            return _row_to_lora_download_job(updated)  # type: ignore[arg-type]

    def mark_interrupted(
        self,
        states: Optional[Iterable[LoraDownloadState]] = None,
    ) -> List[LoraDownloadJobRecord]:
        """应用重启：把非终态任务原子置为 interrupted。返回受影响记录。"""
        conn = self.db.connection
        targets = list(states) if states is not None else sorted(DOWNLOAD_ACTIVE_STATES, key=lambda s: s.value)
        if not targets:
            return []
        marks = ", ".join("?" for _ in targets)
        affected: List[LoraDownloadJobRecord] = []
        now = utcnow_iso()
        with self.db.transaction():
            rows = conn.execute(
                f"SELECT * FROM lora_download_jobs WHERE state IN ({marks})",
                [LoraDownloadState(s).value for s in targets],
            ).fetchall()
            for row in rows:
                assert_download_transition(
                    LoraDownloadState(row["state"]), LoraDownloadState.INTERRUPTED
                )
            conn.execute(
                f"UPDATE lora_download_jobs SET state = 'interrupted', finished_at = ?,"
                f" updated_at = ? WHERE state IN ({marks})",
                [now, now, *(LoraDownloadState(s).value for s in targets)],
            )
            for row in rows:
                updated = _fetch_one(
                    conn, "SELECT * FROM lora_download_jobs WHERE result_id = ?",
                    (row["result_id"],),
                )
                affected.append(_row_to_lora_download_job(updated))  # type: ignore[arg-type]
        if affected:
            try:
                AppEventStore(self.db).record(
                    "lora.downloads_interrupted",
                    {"count": len(affected), "result_ids": [r.result_id for r in affected]},
                )
            except Exception:  # pragma: no cover - 事件写入失败不应阻断恢复
                logger.warning("[WineFoxStorage] 下载中断事件写入失败", exc_info=True)
        return affected

    def delete(self, result_id: str) -> bool:
        conn = self.db.connection
        with self.db.transaction():
            cur = conn.execute(
                "DELETE FROM lora_download_jobs WHERE result_id = ?",
                (str(result_id or "").strip(),),
            )
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# WorkflowRegistry
# ---------------------------------------------------------------------------

class WorkflowRegistry:
    """工作流注册表。name 唯一，register 为 upsert 语义。"""

    def __init__(self, db: Database):
        self.db = db

    def register(
        self,
        name: str,
        definition: Dict[str, Any],
        version: str = "1",
        kind: str = "workflow",
        enabled: bool = True,
    ) -> WorkflowRecord:
        name = str(name or "").strip()
        if not name:
            raise _raise_key_error("name")
        validate_json(definition, name="definition")
        now = utcnow_iso()
        conn = self.db.connection
        with self.db.transaction():
            conn.execute(
                "INSERT INTO workflow_registry (name, version, kind, definition,"
                " enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET"
                " version=excluded.version, kind=excluded.kind,"
                " definition=excluded.definition, enabled=excluded.enabled,"
                " updated_at=excluded.updated_at",
                (name, str(version or "1"), str(kind or "workflow"),
                 json_dumps(definition), 1 if enabled else 0, now, now),
            )
            row = _fetch_one(conn, "SELECT * FROM workflow_registry WHERE name = ?", (name,))
            return _row_to_workflow(row)  # type: ignore[arg-type]

    def get(self, name: str) -> Optional[WorkflowRecord]:
        row = _fetch_one(
            self.db.connection,
            "SELECT * FROM workflow_registry WHERE name = ?",
            (str(name or "").strip(),),
        )
        return _row_to_workflow(row) if row else None

    def list(self, enabled_only: bool = False) -> List[WorkflowRecord]:
        sql = "SELECT * FROM workflow_registry"
        params: list = []
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name"
        return [_row_to_workflow(r) for r in self.db.connection.execute(sql, params).fetchall()]

    def set_enabled(self, name: str, enabled: bool) -> bool:
        conn = self.db.connection
        with self.db.transaction():
            cur = conn.execute(
                "UPDATE workflow_registry SET enabled = ?, updated_at = ? WHERE name = ?",
                (1 if enabled else 0, utcnow_iso(), str(name or "").strip()),
            )
            return cur.rowcount > 0

    def unregister(self, name: str) -> bool:
        conn = self.db.connection
        with self.db.transaction():
            cur = conn.execute(
                "DELETE FROM workflow_registry WHERE name = ?", (str(name or "").strip(),)
            )
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# AppEventStore
# ---------------------------------------------------------------------------

class AppEventStore:
    """追加式应用事件日志。"""

    def __init__(self, db: Database):
        self.db = db

    def record(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> AppEventRecord:
        event_type = str(event_type or "").strip()
        if not event_type:
            raise _raise_key_error("event_type")
        data = dict(payload or {})
        validate_json(data, name="payload")
        now = utcnow_iso()
        conn = self.db.connection
        with self.db.transaction():
            cur = conn.execute(
                "INSERT INTO app_events (event_type, payload, created_at) VALUES (?, ?, ?)",
                (event_type, json_dumps(data), now),
            )
            row = _fetch_one(conn, "SELECT * FROM app_events WHERE id = ?", (cur.lastrowid,))
            return _row_to_app_event(row)  # type: ignore[arg-type]

    def list_events(
        self,
        after_id: Optional[int] = None,
        limit: int = 100,
        event_type: Optional[str] = None,
    ) -> List[AppEventRecord]:
        limit = max(1, min(1000, int(limit)))
        sql = "SELECT * FROM app_events"
        where, params = [], []
        if after_id is not None:
            where.append("id > ?")
            params.append(int(after_id))
        if event_type is not None:
            where.append("event_type = ?")
            params.append(str(event_type))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        return [_row_to_app_event(r) for r in self.db.connection.execute(sql, params).fetchall()]

    def prune(self, before_iso: str) -> int:
        """删除 created_at 早于给定时间的事件；返回删除条数。"""
        conn = self.db.connection
        with self.db.transaction():
            cur = conn.execute(
                "DELETE FROM app_events WHERE created_at < ?", (str(before_iso),)
            )
            return cur.rowcount
