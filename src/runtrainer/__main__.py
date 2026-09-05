"""入口：python -m runtrainer（开发模式；打包入口见 launcher.py）"""
import sys

from .app import run
from .selfcheck import run as selfcheck_run

if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(selfcheck_run())
    run()