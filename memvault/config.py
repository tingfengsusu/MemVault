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
    "asr": {"model": "small", "device": "cpu", "compute_type": "int8"},
    "frames": {"max_frames": 16, "frame_interval": 5.0, "scene_threshold": 0.45},
    "bili": {"quality": 64, "cookies_path": None},
    "keep_video": False,
    "server": {"host": "127.0.0.1", "port": 8765},
    "hotkey": {"enabled": True, "keys": "ctrl+alt+b"},
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
    return _deep_merge(DEFAULTS, loaded)


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
