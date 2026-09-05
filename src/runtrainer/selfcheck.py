"""打包产物自检（不弹窗口）：迁移 + bridge 冒烟。"""
from __future__ import annotations

from . import config
from .api.bridge import Api
from .db import database


def run() -> int:
    config.ensure_dirs()
    config.init_logging()
    database.migrate()
    api = Api()
    s = api.get_settings()
    if not s["ok"]:
        print("SELFCHECK FAIL:", s["error"])
        return 1
    ctx = api.get_goal_wizard_context()
    if not ctx["ok"]:
        print("SELFCHECK FAIL:", ctx["error"])
        return 1
    print("SELFCHECK OK")
    return 0
