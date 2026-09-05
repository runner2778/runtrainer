"""测试环境：数据目录重定向到临时目录（须在导入 runtrainer 前设置）。"""
import os
import sqlite3
import tempfile

import pytest

os.environ["RUNTRAINER_DATA_DIR"] = tempfile.mkdtemp(prefix="runtrainer_test_")


@pytest.fixture(autouse=True)
def _fresh_db():
    """每个用例前：迁移 + 清空全部业务表 + 重置自增序列。

    用关闭外键的裸连接清库（PRAGMA 不能在事务内切换），
    避免父表先删触发 FK 约束。
    """
    from runtrainer.db import database
    database.migrate()
    conn = sqlite3.connect(database.config.DB_PATH)
    conn.execute("PRAGMA foreign_keys = OFF")
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence")
    conn.commit()
    conn.close()
    yield
