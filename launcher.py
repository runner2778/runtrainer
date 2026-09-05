"""PyInstaller 入口脚本（打包后相对导入不可用，故用根级脚本）。"""
import sys

from runtrainer.app import run
from runtrainer.selfcheck import run as selfcheck_run

if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(selfcheck_run())
    run()
