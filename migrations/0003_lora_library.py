# -*- coding: utf-8 -*-
"""迁移 0003：LoRA 库统一数据层。

新增两张表（不改动既有迁移/既有表）：
- ``lora_assets``：本地 LoRA 资产索引（安装/隔离/禁用等），(model_family, filename) 唯一。
  索引与权重分离：删除只删索引，隔离只记录隔离后相对路径，权重文件永不由本层删除。
- ``lora_download_jobs``：下载任务持久化（result_id 唯一），状态机
  queued -> resolving -> downloading -> profiling -> refreshing -> completed，
  终态 completed/failed/cancelled/interrupted；重启时由应用把非终态原子置为 interrupted。

安全约束（本层执行）：任何 JSON 字段不得包含带 token 的下载 URL、Cookie、
Authorization、绝对路径；路径字段一律使用相对路径。
"""

MIGRATION_ID = "0003_lora_library"

STATEMENTS = [
    # ---- lora_assets：本地库资产索引 --------------------------------------
    """
    CREATE TABLE IF NOT EXISTS lora_assets (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        model_family       TEXT NOT NULL,
        filename           TEXT NOT NULL,
        comfy_key          TEXT NOT NULL DEFAULT '',
        display_name       TEXT NOT NULL DEFAULT '',
        source             TEXT NOT NULL DEFAULT 'local',
        model_id           INTEGER,
        version_id         INTEGER,
        preview_key        TEXT,
        state              TEXT NOT NULL DEFAULT 'installed',
        quarantine_relpath TEXT,
        disabled           INTEGER NOT NULL DEFAULT 0,
        disable_reason     TEXT,
        info               TEXT NOT NULL DEFAULT '{}',
        installed_at       TEXT,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        UNIQUE (model_family, filename)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_lora_assets_family
        ON lora_assets (model_family, state)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_lora_assets_updated
        ON lora_assets (updated_at)
    """,
    # ---- lora_download_jobs：下载任务持久化 --------------------------------
    """
    CREATE TABLE IF NOT EXISTS lora_download_jobs (
        result_id        TEXT PRIMARY KEY,
        state            TEXT NOT NULL DEFAULT 'queued',
        family           TEXT NOT NULL DEFAULT 'illustrious',
        alias            TEXT NOT NULL DEFAULT '',
        source           TEXT NOT NULL DEFAULT 'civitai',
        source_ref       TEXT NOT NULL DEFAULT '{}',
        progress         TEXT NOT NULL DEFAULT '{}',
        result           TEXT NOT NULL DEFAULT '{}',
        part_files       TEXT NOT NULL DEFAULT '[]',
        error            TEXT,
        request_timeout  INTEGER NOT NULL DEFAULT 60,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        started_at       TEXT,
        finished_at      TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_lora_download_state
        ON lora_download_jobs (state, updated_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_lora_download_family
        ON lora_download_jobs (family, state)
    """,
]


def upgrade(conn) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)
