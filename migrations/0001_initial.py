# -*- coding: utf-8 -*-
"""迁移 0001：初始 schema。

包含：jobs / history / favorites / lora_profiles / workflow_registry / app_events。
注意：DDL 逐条 conn.execute() 执行（不能用 executescript，它会隐式提交事务，
破坏迁移失败时的回滚语义）。
"""

MIGRATION_ID = "0001_initial"

# 每个语句独立执行，保证整个迁移处于同一事务中
STATEMENTS = [
    # ---- jobs：任务表 ------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        client_request_id  TEXT NOT NULL UNIQUE,
        mode               TEXT NOT NULL,
        state              TEXT NOT NULL DEFAULT 'pending',
        request_json       TEXT NOT NULL DEFAULT '{}',
        comfy_prompt_id    TEXT,
        error              TEXT,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        started_at         TEXT,
        finished_at        TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs (state, updated_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs (created_at)
    """,
    # ---- history：生成历史（软删除，job_id 外键） ---------------------------
    """
    CREATE TABLE IF NOT EXISTS history (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id        INTEGER NOT NULL UNIQUE,
        image_relpath TEXT NOT NULL,
        sha256        TEXT,
        raw_prompt    TEXT NOT NULL DEFAULT '',
        final_prompt  TEXT NOT NULL DEFAULT '',
        seed          INTEGER,
        width         INTEGER,
        height        INTEGER,
        metadata      TEXT NOT NULL DEFAULT '{}',
        deleted_at    TEXT,
        created_at    TEXT NOT NULL,
        FOREIGN KEY (job_id) REFERENCES jobs (id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_history_job ON history (job_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_history_created ON history (created_at, id)
    """,
    # ---- favorites：收藏（scope + ref_key 唯一） -----------------------------
    """
    CREATE TABLE IF NOT EXISTS favorites (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        scope      TEXT NOT NULL,
        ref_key    TEXT NOT NULL,
        kind       TEXT NOT NULL DEFAULT 'generic',
        payload    TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (scope, ref_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_favorites_scope ON favorites (scope, kind)
    """,
    # ---- lora_profiles：LoRA 档案（复合主键） -------------------------------
    """
    CREATE TABLE IF NOT EXISTS lora_profiles (
        model_family  TEXT NOT NULL,
        normalized_key TEXT NOT NULL,
        display_key   TEXT NOT NULL,
        profile       TEXT NOT NULL DEFAULT '{}',
        updated_at    TEXT NOT NULL,
        PRIMARY KEY (model_family, normalized_key)
    )
    """,
    # ---- workflow_registry：工作流注册表 -------------------------------------
    """
    CREATE TABLE IF NOT EXISTS workflow_registry (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL UNIQUE,
        version      TEXT NOT NULL DEFAULT '1',
        kind         TEXT NOT NULL DEFAULT 'workflow',
        definition   TEXT NOT NULL DEFAULT '{}',
        enabled      INTEGER NOT NULL DEFAULT 1,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    )
    """,
    # ---- app_events：应用事件日志（追加式） -----------------------------------
    """
    CREATE TABLE IF NOT EXISTS app_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        payload    TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_app_events_type ON app_events (event_type, id)
    """,
]


def upgrade(conn) -> None:
    """在一个已开启的事务内逐条执行 DDL。"""
    for statement in STATEMENTS:
        conn.execute(statement)
