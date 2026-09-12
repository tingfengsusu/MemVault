"""杂项:ffmpeg 定位。

移植自 Video2Shop(打包逻辑移除,改为 tools/ 目录或 PATH 查找)。
"""
import os
import shutil

from memvault.config import PROJECT_ROOT


def get_ffmpeg_path() -> str:
    """返回可用的 ffmpeg 可执行文件路径。

    优先 <项目>/tools/ffmpeg.exe,其次 PATH。
    """
    local = os.path.join(PROJECT_ROOT, "tools", "ffmpeg.exe")
    if os.path.isfile(local):
        return local
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise FileNotFoundError(
        "未找到 ffmpeg:请将其放入 <项目>/tools/ffmpeg.exe 或加入 PATH"
    )
