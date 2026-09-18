"""路径与配置加载。

数据目录优先级:config.data_dir > 环境变量 MEMVAULT_DATA_DIR > <项目>/data
"""
import os
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

DEFAULTS = {
    "data_dir": None,
    "embedding": {"text_model": "BAAI/bge-small-zh-v1.5", "fake": False},
    "asr": {"model": "small", "device": "auto", "compute_type": "int8",
            "beam_size": 1, "cpu_threads": 0,   # 0=自动;GPU 时用 float16
            "initial_prompt": (
                "以下是一段中文科技/编程类视频的语音,可能包含英文术语与"
                "产品名,例如:API、token、代理、coding plan、CC、ZCode、"
                "Command Code、GitHub、Claude、GPT、CLI、SDK、Whisper、"
                "以及各种 AI 模型名称。请按原词转写。")},
    "frames": {"max_frames": 40, "frame_interval": 5.0, "scene_threshold": 0.45},
    # 视觉:OCR 与图像向量(都随依赖/权重是否就绪自动降级)
    "vision": {"ocr": {"enabled": "auto", "speech_ratio": 0.3},
               "image_embed": {"enabled": "auto"}},
    "bili": {"quality": 64, "cookies_path": None},
    "keep_video": False,
    "server": {"host": "127.0.0.1", "port": 8765},
    "hotkey": {"enabled": True, "keys": "ctrl+alt+b"},
    "llm": {
        "backend": "api",         # api=付费稳定;web=浏览器自动化 chat.deepseek.com 免 token
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key": None,          # 也可用环境变量 DEEPSEEK_API_KEY
        "temperature": 0.1,
        "classify_confidence": 0.8,
        # 广告/推广内容怎么处理:ignore=不写进属性(默认)| mention=单独一条属性
        "ads_policy": "ignore",
        "web": {
            "cookies_file": None,   # 默认 <项目>/config/deepseek_web_auth.json
            "headless": False,
            "timeout_seconds": 180,
        },
    },
    "watch": {"interval_minutes": 30, "max_per_check": 10},
    # 双链阈值在真实库上校准(旧 0.55 + "最高单块"口径会把无关条目连起来):
    # 新口径 = AI 摘要画像文本 + top-3 块相似度均值
    "links": {"similarity_threshold": 0.62, "max_per_item": 3,
              "candidate_chunks": 40},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None) -> dict:
    p = Path(path or os.environ.get("MEMVAULT_CONFIG", DEFAULT_CONFIG_PATH))
    loaded = {}
    if p.exists():
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = _deep_merge(DEFAULTS, loaded)
    cfg["_config_path"] = str(p)  # 供设置页回写
    return cfg


def data_dir(cfg: dict) -> Path:
    d = Path(cfg["data_dir"] or os.environ.get("MEMVAULT_DATA_DIR") or PROJECT_ROOT / "data")
    for sub in ("", "media", "chroma"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def db_path(cfg: dict) -> Path:
    return data_dir(cfg) / "memvault.db"


def chroma_dir(cfg: dict) -> Path:
    return data_dir(cfg) / "chroma"


def media_dir(cfg: dict) -> Path:
    return data_dir(cfg) / "media"
