"""应用配置：数据目录、常量与日志初始化。"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "RunTrainer"  # 数据目录/凭据服务名保持不变（改名会丢数据）
APP_TITLE = "SuperTrainer"
ENGINE_VERSION = "1.2.0"  # 职业双练模式（休息日轻松跑单练，其余每天两练）

DATA_DIR = Path(os.environ.get("RUNTRAINER_DATA_DIR")
                or os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
DB_PATH = DATA_DIR / "runtrainer.db"
RAW_DIR = DATA_DIR / "raw"  # 导入的 FIT/CSV 原文件归档
LOG_DIR = DATA_DIR / "logs"

DEBUG = "--debug" in sys.argv


def web_dir() -> Path:
    """web 前端目录：打包后取 PyInstaller 数据目录，源码运行取仓库内 web/。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "web"
    return Path(__file__).resolve().parents[2] / "web"


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def init_logging() -> None:
    ensure_dirs()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if DEBUG else logging.INFO)
    fh = RotatingFileHandler(LOG_DIR / "runtrainer.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if DEBUG:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
