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

# 推理型模型的 JSON 重试预算上限:思考 token 与正文共用额度,
# 实测 4000 仍可能被思考吃光(真实 #13 提取失败)
_JSON_TOKEN_CEILING = 8000

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
        choice = r.json()["choices"][0]
        self._last_finish_reason = choice.get("finish_reason")
        return choice["message"]["content"] or ""

    def chat_json(self, system: str, user: str, max_tokens: int = 2000) -> dict:
        """JSON 解析。推理型模型(如 deepseek-v4-flash)的思考 token 会占用
        max_tokens 预算,可能返回空内容——此时逐级加大预算重试。"""
        self._last_finish_reason = None
        budget = max_tokens
        text = self.chat(system, user, max_tokens=budget, json_mode=True)
        while self._needs_bigger_budget(text) and budget < _JSON_TOKEN_CEILING:
            prev = budget
            budget = min(max(budget * 2, 800), _JSON_TOKEN_CEILING)
            logger.warning(
                "LLM 返回为空或截断(finish=%s, max_tokens=%d),以 %d 重试",
                self._last_finish_reason, prev, budget)
            text = self.chat(system, user, max_tokens=budget, json_mode=True)
        if not (text or "").strip():
            raise RuntimeError(
                f"LLM 返回空内容(finish={self._last_finish_reason}, "
                f"max_tokens={budget});可能是额度/限流问题,请稍后重试")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start:end + 1])
            raise

    def _needs_bigger_budget(self, text: str) -> bool:
        """内容为空,或(推理模型)思考吃光预算导致截断。"""
        return not (text or "").strip() or self._last_finish_reason == "length"


# ── 后端工厂:api(付费稳定) / web(免 token 网页自动化)────────────────
_web_client = None


def get_llm_client(cfg: dict):
    """按 cfg.llm.backend 返回客户端;web 后端进程内复用同一浏览器实例。"""
    global _web_client
    backend = (cfg.get("llm", {}) or {}).get("backend", "api")
    if backend == "web":
        if _web_client is None:
            from memvault.llm_web import WebLLMClient

            _web_client = WebLLMClient(cfg)
        return _web_client
    return LLMClient(cfg)


def reset_llm_client():
    """设置页切换后端后调用:关闭并重建缓存的 web 浏览器实例。"""
    global _web_client
    if _web_client is not None:
        try:
            _web_client.close()
        except Exception:  # noqa: BLE001
            pass
        _web_client = None
