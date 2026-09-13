"""计划任务自启入口:pythonw run_tray.py 无窗口运行。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memvault.tray import run  # noqa: E402

if __name__ == "__main__":
    run()
