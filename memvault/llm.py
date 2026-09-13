"""LLM 客户端(OpenAI 兼容,默认 DeepSeek)。

API key 解析顺序:config.llm.api_key → 环境变量 DEEPSEEK_API_KEY
→ MemVault/.env → Video2Shop/config/config.yaml(复用前作密钥,仅本机)。
任何失败都使 enabled=False,调用方必须走"无 LLM 降级"路径。
"""
import json
import logging
import os
from pathlib import Path

import requests

from memvault.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

_VIDEO2SHOP_CONFIG = Path("D:/Code/Video2Shop/config/config.yaml")


def _load_dotenv(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def resolve_api_key(cfg: dict) -> str | None:
    key = cfg.get("llm", {}).get("api_key")
    if key:
        return key
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    env = _load_dotenv(PROJECT_ROOT / ".env")
    if env.get("DEEPSEEK_API_KEY"):
        return env["DEEPSEEK_API_KEY"]
    # 复用前作密钥(本机开发便利;不存在则跳过)
    if _VIDEO2SHOP_CONFIG.exists():
        try:
            import yaml

            v2s = yaml.safe_load(_VIDEO2SHOP_CONFIG.read_text(encoding="utf-8")) or {}
            key = (v2s.get("deepseek") or {}).get("api_key")
            if key:
                logger.info("LLM key 复用自 Video2Shop 配置")
                return key
        except Exception as e:  # noqa: BLE001
            logger.warning("读取 Video2Shop 配置失败:%s", e)
    return None


class LLMClient:
    """chat() 返回文本;chat_json() 解析 JSON(容错提取代码块/前后缀)。"""

    def __init__(self, cfg: dict):
        l = cfg.get("llm", {})
        self.base_url = l.get("base_url", "https://api.deepseek.com/v1").rstrip("/")
        self.model = l.get("model", "deepseek-chat")
        self.temperature = l.get("temperature", 0.1)
        self.api_key = resolve_api_key(cfg)
        self.enabled = bool(self.api_key)
        if not self.enabled:
            logger.warning("未配置 LLM api_key,自动分类/提取将跳过"
                           "(设置环境变量 DEEPSEEK_API_KEY 或 config.llm.api_key)")

    def chat(self, system: str, user: str, max_tokens: int = 2000,
             json_mode: bool = False) -> str:
        if not self.enabled:
            raise RuntimeError("LLM 未配置")
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body,
            timeout=90,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def chat_json(self, system: str, user: str, max_tokens: int = 2000) -> dict:
        text = self.chat(system, user, max_tokens=max_tokens, json_mode=True)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start:end + 1])
            raise
