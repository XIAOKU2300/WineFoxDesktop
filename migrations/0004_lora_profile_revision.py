# -*- coding: utf-8 -*-
"""迁移 0004：lora_profiles 档案版本号。

新增 ``revision`` 列：每次修改档案 +1（INSERT=1，upsert 冲突时递增），
用于匹配/LLM 缓存的失效键（按 family + revision 聚合失效）。

安全约束（本层执行）：revision 仅作为失效依据，不携带任何 URL/凭据。
"""

MIGRATION_ID = "0004_lora_profile_revision"

STATEMENTS = [
    """
    ALTER TABLE lora_profiles ADD COLUMN revision INTEGER NOT NULL DEFAULT 0
    """,
]


def upgrade(conn) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)
