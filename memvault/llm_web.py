"""网页版 LLM 通道(免 token 消耗):浏览器自动化驱动的 chat.deepseek.com。

- 首次使用在弹出的 Chrome 里手动登录一次,登录态保存到 cookies 文件;
- 接口与 LLMClient 对齐(chat / chat_json),供自动分类、提取、聊天复用;
- 调用串行化(单页面,一把锁),比 API 慢很多,适合成本敏感场景;
- 仅本地使用:浏览器自动化可能受网站 UI 改版影响,失败时回退 API 后端即可。
"""
import json
import logging
import re
import threading
import time
from pathlib import Path

from memvault.config import PROJECT_ROOT

logger = logging.getLogger(__name__)


class WebLLMClient:
    """免 token 的网页 LLM 通道。懒启动浏览器,登录态持久化。"""

    def __init__(self, cfg: dict):
        w = (cfg.get("llm", {}) or {}).get("web", {}) or {}
        self.model = w.get("model_name", "deepseek-web")
        self.timeout = int(w.get("timeout_seconds", 180))
        self.headless = bool(w.get("headless", False))
        cookies = w.get("cookies_file")
        self.cookies_file = Path(cookies) if cookies else (
            PROJECT_ROOT / "config" / "deepseek_web_auth.json")
        self.enabled = True  # 可用性在首次真实调用时验证(可能需要登录)
        self._lock = threading.Lock()
        self._pw = self.browser = self.context = self.page = None

    # ── 浏览器生命周期(移植自 Video2Shop deepseek_web_analyzer)──────
    def _launch_chrome(self, launch_args: dict):
        try:
            return self._pw.chromium.launch(channel="chrome", **launch_args)
        except Exception as e1:  # noqa: BLE001
            logger.warning("channel='chrome' 启动失败: %s", e1)
        import platform

        candidates = []
        if platform.system() == "Windows":
            import os

            candidates = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(
                    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            ]
        for path in candidates:
            if Path(path).exists():
                try:
                    return self._pw.chromium.launch(
                        executable_path=path, **launch_args)
                except Exception as e2:  # noqa: BLE001
                    logger.warning("executable_path=%s 失败: %s", path, e2)
        raise RuntimeError("无法启动 Chrome:请确认已安装 Google Chrome")

    def _ensure_browser(self):
        if self.page is not None:
            return
        from playwright.sync_api import sync_playwright

        logger.info("[web通道] 启动 Chrome ...")
        self._pw = sync_playwright().start()
        self.browser = self._launch_chrome({
            "headless": self.headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        })
        if self.cookies_file.exists():
            self.context = self.browser.new_context(
                storage_state=str(self.cookies_file))
        else:
            self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        self._ensure_logged_in()

    def _ensure_logged_in(self):
        self.page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded")
        try:
            self.page.wait_for_selector(
                'textarea, [contenteditable="true"], [role="textbox"]',
                timeout=8000)
            logger.info("[web通道] 登录态有效")
            return
        except Exception:  # noqa: BLE001 — 需要登录
            pass
        logger.info("=" * 50)
        logger.info("⚠ [web通道] 请在打开的浏览器中完成 DeepSeek 登录(扫码/手机号)")
        logger.info("  登录后程序自动继续;登录态会保存,下次免登录")
        logger.info("=" * 50)
        self.page.wait_for_selector(
            'textarea, [contenteditable="true"], [role="textbox"]',
            timeout=300000)
        self.cookies_file.parent.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(self.cookies_file))
        logger.info("[web通道] 登录成功,登录态已保存: %s", self.cookies_file)

    def close(self):
        for obj, method in ((self.context, "close"), (self.browser, "close")):
            if obj:
                try:
                    getattr(obj, method)()
                except Exception:  # noqa: BLE001
                    pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self.browser = self.context = self.page = None

    # ── 对话 ──────────────────────────────────────────────────────────
    def chat(self, system: str, user: str, max_tokens: int = 2000,
             json_mode: bool = False) -> str:
        with self._lock:  # 单页面串行
            self._ensure_browser()
            self.page.goto("https://chat.deepseek.com/",
                           wait_until="domcontentloaded")
            time.sleep(1.0)
            prompt = f"{system}\n\n{user}"
            if json_mode:
                prompt += "\n\n只输出 JSON,不要输出任何其他内容。"
            self._fill_and_send(prompt)
            text = self._wait_for_response()
            try:  # 刷新登录态
                self.context.storage_state(path=str(self.cookies_file))
            except Exception:  # noqa: BLE001
                pass
            return text

    def chat_json(self, system: str, user: str, max_tokens: int = 2000) -> dict:
        text = self.chat(system, user, max_tokens=max_tokens, json_mode=True)
        result = self._extract_json(text)
        if result is None:
            raise ValueError(f"网页回复中未找到 JSON: {text[:150]}")
        return result

    def _fill_and_send(self, prompt: str):
        input_box = None
        for sel in ('textarea', '[contenteditable="true"]', '[role="textbox"]'):
            try:
                el = self.page.locator(sel).first
                if el.is_visible(timeout=3000):
                    input_box = el
                    break
            except Exception:  # noqa: BLE001
                continue
        if input_box is None:
            raise RuntimeError("[web通道] 未找到聊天输入框(页面结构可能已改版)")
        input_box.click()
        time.sleep(0.3)
        input_box.fill(prompt)
        time.sleep(0.5)
        # DeepSeek 发送按钮是 div[role=button],Enter 不发送 → 逐个候选点击
        for sel in ('div[role="button"][aria-disabled="false"]',
                    'div[role="button"]:has(svg)',
                    'button[aria-label="发送"]'):
            try:
                loc = self.page.locator(sel)
                for idx in range(loc.count() - 1, -1, -1):
                    btn = loc.nth(idx)
                    try:
                        if btn.is_visible(timeout=1000):
                            btn.click(timeout=3000)
                            return
                    except Exception:  # noqa: BLE001
                        continue
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError("[web通道] 未找到发送按钮")

    def _wait_for_response(self) -> str:
        selectors = ('.ds-markdown', '[class*="markdown"]',
                     '[class*="message"] [class*="content"]')
        last_len, stable, start = 0, 0, time.time()
        while time.time() - start < self.timeout:
            time.sleep(1.2)
            for sel in selectors:
                try:
                    els = self.page.locator(sel)
                    if els.count() == 0:
                        continue
                    text = els.last.inner_text()
                    cur = len(text or "")
                    if cur > 5:
                        if cur == last_len:
                            stable += 1
                            if stable >= 2:
                                logger.info("[web通道] 回复完成(%d 字符,%.1fs)",
                                            cur, time.time() - start)
                                return text
                        else:
                            stable, last_len = 0, cur
                        break
                except Exception:  # noqa: BLE001
                    continue
        for sel in selectors:  # 超时回退
            try:
                els = self.page.locator(sel)
                if els.count() > 0:
                    return els.last.inner_text()
            except Exception:  # noqa: BLE001
                continue
        raise TimeoutError(f"[web通道] 等待回复超时({self.timeout}s)")

    @staticmethod
    def _extract_json(text: str):
        if not text:
            return None
        text = text.strip()
        for candidate in (text,
                          (re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
                           or [None, None])[1]):
            if not candidate:
                continue
            try:
                return json.loads(candidate.strip())
            except json.JSONDecodeError:
                pass
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return None
