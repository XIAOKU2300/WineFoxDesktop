"""房间 SQLite 存储（独立于主后端 winefox.db）。

规范：
- 5 张表：rooms / room_members / room_messages / room_attachments / room_jobs。
- 所有文件路径只保存相对路径（相对房间数据目录）。
- 所有 JSON 写入前验证可序列化；所有写入使用显式事务。
- WAL + foreign_keys=ON + busy_timeout=5000；写事务 BEGIN IMMEDIATE 抢占写锁。
- room_attachments 增加 message_id 列，把一条消息（如 generation_result）的
  多个附件与消息关联（规范要求“结果以一条消息的多个附件形式出现”）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from . import protocol

_BUSY_TIMEOUT_MS = 5000
_SCHEMA_VERSION = 1


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_public_id() -> str:
    # 32 字符字母表（去掉易混淆的 l/o/0/1）生成 16 字符 public id（随邀请链接展示）。
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    return "".join(alphabet[(uuid.uuid4().int >> (i * 5)) & 0x1F] for i in range(16))


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS rooms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  public_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  owner_name TEXT NOT NULL,
  password_algorithm TEXT NOT NULL,
  password_parameters_json TEXT NOT NULL,
  password_salt TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  state TEXT NOT NULL,
  max_members INTEGER NOT NULL DEFAULT 8,
  allow_chat INTEGER NOT NULL DEFAULT 1,
  allow_upload INTEGER NOT NULL DEFAULT 1,
  allow_generation INTEGER NOT NULL DEFAULT 1,
  allow_img2img INTEGER NOT NULL DEFAULT 1,
  allow_prompt_enhancement INTEGER NOT NULL DEFAULT 1,
  allowed_model_families_json TEXT NOT NULL DEFAULT '[]',
  allowed_workflow_ids_json TEXT NOT NULL DEFAULT '[]',
  allowed_loras_json TEXT NOT NULL DEFAULT '[]',
  per_member_queue_limit INTEGER NOT NULL DEFAULT 1,
  room_queue_limit INTEGER NOT NULL DEFAULT 10,
  message_retention_days INTEGER NOT NULL DEFAULT 7,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  closed_at TEXT
);

CREATE TABLE IF NOT EXISTS room_members (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  display_name TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  role TEXT NOT NULL,
  session_hash TEXT NOT NULL,
  joined_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  disconnected_at TEXT,
  kicked_at TEXT,
  UNIQUE(room_id, normalized_name)
);

CREATE TABLE IF NOT EXISTS room_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  sender_id INTEGER,
  client_message_id TEXT,
  type TEXT NOT NULL,
  text TEXT,
  reply_to_id INTEGER,
  generation_job_id INTEGER,
  created_at TEXT NOT NULL,
  deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS room_attachments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  uploader_id INTEGER,
  message_id INTEGER,
  kind TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  thumbnail_relpath TEXT,
  mime_type TEXT,
  size INTEGER,
  width INTEGER,
  height INTEGER,
  sha256 TEXT,
  created_at TEXT NOT NULL,
  deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS room_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  member_id INTEGER,
  core_job_id TEXT,
  client_request_id TEXT NOT NULL UNIQUE,
  request_snapshot_json TEXT,
  state TEXT NOT NULL,
  queue_position INTEGER,
  created_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_members_room ON room_members(room_id);
CREATE INDEX IF NOT EXISTS idx_members_session ON room_members(session_hash);
CREATE INDEX IF NOT EXISTS idx_messages_room ON room_messages(room_id, id);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON room_attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_jobs_room ON room_jobs(room_id);
CREATE INDEX IF NOT EXISTS idx_jobs_client ON room_jobs(client_request_id);
"""


class RoomStorage:
    """一个房间一个存储实例，绑定独立 SQLite 数据库文件。"""

    def __init__(self, db_path: Path, *, migrate: bool = True) -> None:
        self.db_path = db_path
        self._write_lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=_BUSY_TIMEOUT_MS / 1000)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        self._schema_version = 0
        if migrate:
            self._migrate()

    def _migrate(self) -> None:
        with self._write_lock:
            with self._conn:
                self._conn.executescript(_SCHEMA)
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            self._schema_version = version
        # user_version 记录到当前模式版本
        if version < _SCHEMA_VERSION:
            with self._write_lock:
                with self._conn:
                    self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    def close(self) -> None:
        with self._write_lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # 事务
    # ------------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            with self._conn:
                yield self._conn

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._write_lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchone()

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._write_lock:
            cur = self._conn.execute(sql, params)
            return list(cur.fetchall())

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._write_lock:
            with self._conn:
                self._conn.execute(sql, params)

    # ------------------------------------------------------------------
    # Rooms
    # ------------------------------------------------------------------
    def create_room(self, *, public_id: str, name: str, owner_name: str,
                    password_algorithm: str, password_parameters_json: str,
                    password_salt: str, password_hash: str,
                    settings: dict[str, Any]) -> int:
        now = utcnow_iso()
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO rooms (public_id, name, owner_name, password_algorithm,"
                " password_parameters_json, password_salt, password_hash, state, max_members,"
                " allow_chat, allow_upload, allow_generation, allow_img2img,"
                " allow_prompt_enhancement, allowed_model_families_json,"
                " allowed_workflow_ids_json, allowed_loras_json, per_member_queue_limit,"
                " room_queue_limit, message_retention_days, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    public_id, name, owner_name, password_algorithm, password_parameters_json,
                    password_salt, password_hash, protocol.ROOM_STATE_CREATED,
                    int(settings.get("maxMembers", 8)),
                    1 if settings.get("allowChat", True) else 0,
                    1 if settings.get("allowUpload", True) else 0,
                    1 if settings.get("allowGeneration", True) else 0,
                    1 if settings.get("allowImg2img", True) else 0,
                    1 if settings.get("allowPromptEnhancement", True) else 0,
                    json.dumps(settings.get("allowedModelFamilies", []), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(settings.get("allowedWorkflowIds", []), ensure_ascii=False, separators=(",", ":")),
                    json.dumps(settings.get("allowedLoras", []), ensure_ascii=False, separators=(",", ":")),
                    int(settings.get("perMemberQueueLimit", 1)),
                    int(settings.get("roomQueueLimit", 10)),
                    int(settings.get("messageRetentionDays", 7)),
                    now,
                ),
            )
            room_id = int(cur.lastrowid)
        return room_id

    def set_room_state(self, room_id: int, state: str) -> None:
        now = utcnow_iso()
        closed = now if state == protocol.ROOM_STATE_CLOSED else None
        self.execute("UPDATE rooms SET state=?, closed_at=? WHERE id=?", (state, closed, room_id))

    def update_room_password(self, room_id: int, *, password_algorithm: str,
                             password_parameters_json: str, password_salt: str,
                             password_hash: str) -> None:
        self.execute(
            "UPDATE rooms SET password_algorithm=?, password_parameters_json=?,"
            " password_salt=?, password_hash=? WHERE id=?",
            (password_algorithm, password_parameters_json, password_salt, password_hash, room_id),
        )

    def get_room(self, room_id: int) -> sqlite3.Row | None:
        return self.fetchone("SELECT * FROM rooms WHERE id=?", (room_id,))

    def room_by_public_id(self, public_id: str) -> sqlite3.Row | None:
        return self.fetchone("SELECT * FROM rooms WHERE public_id=?", (public_id,))

    def room_settings_map(self, room_id: int) -> dict[str, Any]:
        row = self.get_room(room_id)
        if row is None:
            return {}
        return {
            "id": row["id"],
            "publicId": row["public_id"],
            "name": row["name"],
            "ownerName": row["owner_name"],
            "state": row["state"],
            "maxMembers": row["max_members"],
            "allowChat": bool(row["allow_chat"]),
            "allowUpload": bool(row["allow_upload"]),
            "allowGeneration": bool(row["allow_generation"]),
            "allowImg2img": bool(row["allow_img2img"]),
            "allowPromptEnhancement": bool(row["allow_prompt_enhancement"]),
            "allowedModelFamilies": json.loads(row["allowed_model_families_json"] or "[]"),
            "allowedWorkflowIds": json.loads(row["allowed_workflow_ids_json"] or "[]"),
            "allowedLoras": json.loads(row["allowed_loras_json"] or "[]"),
            "perMemberQueueLimit": row["per_member_queue_limit"],
            "roomQueueLimit": row["room_queue_limit"],
            "messageRetentionDays": row["message_retention_days"],
            "createdAt": row["created_at"],
        }

    # ------------------------------------------------------------------
    # Members
    # ------------------------------------------------------------------
    def add_member(self, *, room_id: int, display_name: str, normalized_name: str,
                   role: str, session_hash: str) -> int:
        now = utcnow_iso()
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO room_members (room_id, display_name, normalized_name, role,"
                " session_hash, joined_at, last_seen_at) VALUES (?,?,?,?,?,?,?)",
                (room_id, display_name, normalized_name, role, session_hash, now, now),
            )
            return int(cur.lastrowid)

    def member_by_session(self, session_hash: str) -> sqlite3.Row | None:
        return self.fetchone("SELECT * FROM room_members WHERE session_hash=?", (session_hash,))

    def get_member(self, member_id: int) -> sqlite3.Row | None:
        return self.fetchone("SELECT * FROM room_members WHERE id=?", (member_id,))

    def name_taken(self, room_id: int, normalized_name: str) -> bool:
        row = self.fetchone(
            "SELECT id FROM room_members WHERE room_id=? AND normalized_name=?", (room_id, normalized_name)
        )
        return row is not None

    def active_members(self, room_id: int) -> list[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM room_members WHERE room_id=? AND kicked_at IS NULL ORDER BY joined_at", (room_id,)
        )

    def active_member_count(self, room_id: int) -> int:
        row = self.fetchone(
            "SELECT COUNT(*) AS c FROM room_members WHERE room_id=? AND kicked_at IS NULL", (room_id,)
        )
        return int(row["c"]) if row else 0

    def touch_last_seen(self, member_id: int) -> None:
        self.execute("UPDATE room_members SET last_seen_at=? WHERE id=?", (utcnow_iso(), member_id))

    def mark_disconnected(self, member_id: int) -> None:
        self.execute("UPDATE room_members SET disconnected_at=? WHERE id=?", (utcnow_iso(), member_id))

    def kick_member(self, member_id: int) -> None:
        self.execute("UPDATE room_members SET kicked_at=? WHERE id=?", (utcnow_iso(), member_id))

    # ------------------------------------------------------------------
    # Messages（幂等：client_message_id 唯一；分页按 id 倒序）
    # ------------------------------------------------------------------
    def add_message(self, *, room_id: int, sender_id: int | None, msg_type: str,
                    text: str | None, client_message_id: str | None = None,
                    reply_to_id: int | None = None, generation_job_id: int | None = None,
                    created_at: str | None = None) -> int:
        now = created_at or utcnow_iso()
        if client_message_id:
            existing = self.fetchone(
                "SELECT id FROM room_messages WHERE room_id=? AND client_message_id=?", (room_id, client_message_id)
            )
            if existing is not None:
                return int(existing["id"])
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO room_messages (room_id, sender_id, client_message_id, type, text,"
                " reply_to_id, generation_job_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (room_id, sender_id, client_message_id, msg_type, text, reply_to_id, generation_job_id, now),
            )
            return int(cur.lastrowid)

    def get_message(self, message_id: int) -> sqlite3.Row | None:
        return self.fetchone(
            "SELECT * FROM room_messages WHERE id=? AND deleted_at IS NULL", (message_id,)
        )

    def list_messages(self, *, room_id: int, before: int | None = None, limit: int = 50) -> list[sqlite3.Row]:
        limit = min(max(int(limit), 1), 100)
        sql = "SELECT * FROM room_messages WHERE room_id=? AND deleted_at IS NULL"
        params: list[Any] = [room_id]
        if before is not None:
            sql += " AND id < ?"
            params.append(int(before))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self.fetchall(sql, tuple(params))

    def messages_after(self, *, room_id: int, after_id: int | None = None, limit: int = 200) -> list[sqlite3.Row]:
        sql = "SELECT * FROM room_messages WHERE room_id=? AND deleted_at IS NULL"
        params: list[Any] = [room_id]
        if after_id is not None:
            sql += " AND id > ?"
            params.append(int(after_id))
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        return self.fetchall(sql, tuple(params))

    def mark_message_deleted(self, message_id: int) -> None:
        self.execute("UPDATE room_messages SET deleted_at=? WHERE id=?", (utcnow_iso(), message_id))

    def clear_messages(self, room_id: int) -> int:
        """软删除房间全部消息，返回受影响行数。"""
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE room_messages SET deleted_at=? WHERE room_id=? AND deleted_at IS NULL",
                (utcnow_iso(), room_id),
            )
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------
    def add_attachment(self, *, room_id: int, uploader_id: int | None, message_id: int | None,
                       kind: str, relative_path: str, thumbnail_relpath: str | None,
                       mime_type: str | None, size: int, width: int | None, height: int | None,
                       sha256: str) -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO room_attachments (room_id, uploader_id, message_id, kind, relative_path,"
                " thumbnail_relpath, mime_type, size, width, height, sha256, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (room_id, uploader_id, message_id, kind, relative_path, thumbnail_relpath,
                 mime_type, size, width, height, sha256, utcnow_iso()),
            )
            return int(cur.lastrowid)

    def get_attachment(self, attachment_id: int) -> sqlite3.Row | None:
        return self.fetchone(
            "SELECT * FROM room_attachments WHERE id=? AND deleted_at IS NULL", (attachment_id,)
        )

    def attachments_for_message(self, message_id: int) -> list[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM room_attachments WHERE message_id=? AND deleted_at IS NULL ORDER BY id", (message_id,)
        )

    def link_attachments_to_message(self, attachment_ids: list[int], message_id: int) -> None:
        if not attachment_ids:
            return
        with self.tx() as conn:
            for aid in attachment_ids:
                conn.execute("UPDATE room_attachments SET message_id=? WHERE id=?", (message_id, aid))

    # ------------------------------------------------------------------
    # Jobs（幂等：client_request_id 唯一）
    # ------------------------------------------------------------------
    def create_job(self, *, room_id: int, member_id: int, client_request_id: str,
                   request_snapshot: Any) -> tuple[int, bool]:
        """创建任务，返回 (job_id, is_new)。重复的 client_request_id 返回已有任务。"""
        existing = self.fetchone(
            "SELECT id FROM room_jobs WHERE client_request_id=?", (client_request_id,)
        )
        if existing is not None:
            return int(existing["id"]), False
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO room_jobs (room_id, member_id, client_request_id, request_snapshot_json,"
                " state, queue_position, created_at) VALUES (?,?,?,?,?,?,?)",
                (room_id, member_id, client_request_id,
                 json.dumps(request_snapshot, ensure_ascii=False, separators=(",", ":")),
                 protocol.JOB_STATE_QUEUED, None, utcnow_iso()),
            )
            return int(cur.lastrowid), True

    def get_job_by_client_id(self, client_request_id: str) -> sqlite3.Row | None:
        return self.fetchone("SELECT * FROM room_jobs WHERE client_request_id=?", (client_request_id,))

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        return self.fetchone("SELECT * FROM room_jobs WHERE id=?", (job_id,))

    def set_job_state(self, job_id: int, state: str, *, core_job_id: str | None = None,
                      queue_position: int | None = None, completed_at: str | None = None) -> None:
        completed = completed_at or (utcnow_iso() if state in (
            protocol.JOB_STATE_COMPLETED, protocol.JOB_STATE_FAILED, protocol.JOB_STATE_CANCELLED
        ) else None)
        with self.tx() as conn:
            if core_job_id is not None:
                conn.execute("UPDATE room_jobs SET state=?, core_job_id=?, queue_position=?, completed_at=? WHERE id=?",
                             (state, core_job_id, queue_position, completed, job_id))
            else:
                conn.execute("UPDATE room_jobs SET state=?, queue_position=?, completed_at=? WHERE id=?",
                             (state, queue_position, completed, job_id))

    def room_running_jobs(self, room_id: int) -> int:
        row = self.fetchone(
            "SELECT COUNT(*) AS c FROM room_jobs WHERE room_id=? AND state IN ('queued','running')", (room_id,)
        )
        return int(row["c"]) if row else 0

    def member_running_jobs(self, room_id: int, member_id: int) -> int:
        row = self.fetchone(
            "SELECT COUNT(*) AS c FROM room_jobs WHERE room_id=? AND member_id=? AND state IN ('queued','running')",
            (room_id, member_id),
        )
        return int(row["c"]) if row else 0

    def list_room_jobs(self, room_id: int, member_id: int | None = None, limit: int = 50) -> list[sqlite3.Row]:
        limit = max(1, min(200, int(limit)))
        if member_id is not None:
            return self.fetchall(
                "SELECT * FROM room_jobs WHERE room_id=? AND member_id=? ORDER BY id DESC LIMIT ?",
                (room_id, member_id, limit),
            )
        return self.fetchall(
            "SELECT * FROM room_jobs WHERE room_id=? ORDER BY id DESC LIMIT ?", (room_id, limit)
        )
