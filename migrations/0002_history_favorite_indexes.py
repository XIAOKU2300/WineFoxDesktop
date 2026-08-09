# -*- coding: utf-8 -*-
"""迁移 0002：为历史分页与收藏查询补充索引。

用于验证增量迁移路径（0001 之后追加，不影响旧数据）。
"""

MIGRATION_ID = "0002_history_favorite_indexes"

STATEMENTS = [
    # 历史按创建时间倒序游标分页 + 软删除过滤
    """
    CREATE INDEX IF NOT EXISTS idx_history_page
        ON history (deleted_at, created_at DESC, id DESC)
    """,
    # 收藏唯一性查询
    """
    CREATE INDEX IF NOT EXISTS idx_favorites_ref
        ON favorites (scope, ref_key)
    """,
]


def upgrade(conn) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)
