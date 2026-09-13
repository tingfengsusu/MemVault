r"""
购物平台自动化模块 — 通过 Playwright CDP 连接自动启动/复用 Chrome 浏览器。

类层次:
  PlatformHandler (ABC)       — 抽象基类
    └── JdHandler             — 京东实现

工作流:
  1. 程序尝试连接 localhost:9222（已运行的调试模式 Chrome）。
  2. 若连接失败，自动启动一个带 --remote-debugging-port 的 Chrome 子进程。
  3. 通过 connect_over_cdp 连接，复用已有登录态。
  4. 依次搜索关键词 → 点击第一个"加入购物车"按钮。
"""

import logging
import os
import platform
import shutil

import time
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)


class PlatformHandler(ABC):
    """购物平台处理器抽象基类。"""

    @abstractmethod
    def login(self):
        """确保平台已登录。"""
        pass

    @abstractmethod
    def search_and_add(self, keyword: str) -> bool:
        """搜索关键词并加入购物车。

        返回:
            True  加购成功
            False 加购失败（搜索无结果、无加购按钮等）
        """
        pass

    @abstractmethod
    def close(self):
        """断开连接，清理资源。"""
        pass


class JdHandler(PlatformHandler):
    """京东购物车自动化处理器。

    通过 CDP 协议连接到 Chrome 调试实例：
      - 优先复用已运行的调试模式 Chrome（端口 9222）
      - 若未运行则自动启动一个带调试端口的 Chrome 子进程
      - 登录态由 user-data-dir 持久化，无需管理 cookies
    """

    def __init__(
        self,
        debug_port: int = 9222,
        auto_launch: bool = True,
        close_browser_on_exit: bool = False,
        user_data_dir: str = None,
        search_timeout: int = 10000,
        add_cart_retry: int = 2,
        add_cart_delay: float = 0.5,
        click_retry: int = 1,
        search_wait_after: float = 1.0,
        retry_on_failure: bool = True,
        chrome_path: str = None,
        browser_type: str = None,
    ):
        self.debug_port = debug_port
        self.cdp_url = f"http://localhost:{debug_port}"
        self.auto_launch = auto_launch
        self.close_browser_on_exit = close_browser_on_exit
        self.search_timeout = search_timeout
        self.add_cart_retry = add_cart_retry
        self.add_cart_delay = add_cart_delay
        self.click_retry = click_retry
        self.search_wait_after = search_wait_after
        self.retry_on_failure = retry_on_failure
        self.chrome_path = chrome_path
        self.browser_type = browser_type

        # 默认 user_data_dir 放在 temp 目录下
        if user_data_dir is None:
            self.user_data_dir = str(
                Path(os.environ.get("TEMP", "/tmp")) / "chrome-playwright-profile"
            )
        else:
            self.user_data_dir = user_data_dir

        self._pw = None
        self.browser = None
        self.page = None
        self._logged_in = False
        self._launched_by_us = False  # 标记是否由本程序启动
        self._launched_browser = None  # Playwright launch_persistent_context 返回值
        self._launched_context = None  # 同上，语义别名

    # ── 查找 Chrome 可执行文件 ──────────────────────────────────────────

    @staticmethod
    def _find_chrome_exe() -> str:
        """跨平台查找 Chrome/Chromium 可执行文件路径。"""
        system = platform.system()

        if system == "Windows":
            candidates = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(
                    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
                ),
            ]
        elif system == "Darwin":
            candidates = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ]
        else:  # Linux
            candidates = [
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
                "/snap/bin/chromium",
            ]

        # 先检查已知路径
        for path in candidates:
            if Path(path).exists():
                logger.info(f"找到 Chrome: {path}")
                return path

        # 回退到 shutil.which
        for name in ["chrome", "google-chrome", "google-chrome-stable",
                      "chromium", "chromium-browser"]:
            found = shutil.which(name)
            if found:
                logger.info(f"通过 PATH 找到: {found}")
                return found

        raise FileNotFoundError(
            "未找到 Chrome 可执行文件。请确认系统已安装 Google Chrome。\n"
            f"已搜索的路径: {candidates}"
        )

    # ── 连接浏览器调试端口 ────────────────────────────────────────────

    def _try_connect(self):
        """尝试通过 CDP 连接到 Chrome 调试端口，返回 True/False。"""
        try:
            logger.info(f"尝试连接 Chrome 调试端口: {self.cdp_url}")
            self.browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
            logger.info("CDP 连接成功")
            return True
        except Exception as e:
            logger.info(f"CDP 连接失败: {e}")
            return False

    def _launch_chrome_debug(self):
        """通过 Playwright 启动浏览器（优先使用 channel，其次 executable_path）。

        浏览器启动优先级:
          1. chrome_path 非空且文件存在 → executable_path=chrome_path
          2. browser_type 为已知类型 → channel="chrome"/"msedge"/"chromium"
          3. 回退 → executable_path=<自动查找 Chrome>
        """
        args = [
            f"--remote-debugging-port={self.debug_port}",
            "--no-first-run",
            "--no-default-browser-check",
        ]

        launch_kwargs = {"headless": False, "args": args}

        if self.chrome_path and Path(self.chrome_path).exists():
            launch_kwargs["executable_path"] = self.chrome_path
            logger.info(f"使用自定义浏览器路径: {self.chrome_path}")
        elif self.browser_type:
            channel_map = {
                "Google Chrome": "chrome",
                "Microsoft Edge": "msedge",
                "Chromium": "chromium",
            }
            channel = channel_map.get(self.browser_type)
            if channel:
                launch_kwargs["channel"] = channel
                logger.info(f"使用 Playwright channel: {channel} ({self.browser_type})")
            else:
                logger.warning(
                    f"不支持的浏览器类型: {self.browser_type}，回退到自动查找 Chrome"
                )
                launch_kwargs["executable_path"] = self._find_chrome_exe()
        else:
            launch_kwargs["executable_path"] = self._find_chrome_exe()
            logger.info("使用自动查找的 Chrome")

        logger.info("正在启动浏览器...")
        try:
            self._launched_browser = self._pw.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                **launch_kwargs,
            )
            self._launched_by_us = True
            logger.info("浏览器已启动")
        except Exception as e:
            hint = ""
            if "channel" in launch_kwargs:
                hint = (
                    f"\n请确认已安装 {self.browser_type}。"
                    f"\nPlaywright 支持的系统渠道: chrome, msedge, chromium。"
                )
            raise RuntimeError(f"启动浏览器失败: {e}{hint}")

    def _connect_browser(self):
        """连接到浏览器（CDP 复用 / Playwright 启动）。

        流程:
          1. 先尝试 CDP 连接已运行的调试端口（复用登录态）
          2. 失败且 auto_launch=True → Playwright 启动浏览器 → 直接使用
          3. 全部失败 → 抛出 RuntimeError
        """
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()

        # 第一次尝试：连接可能已运行的浏览器
        if self._try_connect():
            self._setup_page()
            return

        # 连接失败，尝试自动启动
        if not self.auto_launch:
            raise RuntimeError(
                f"无法连接到浏览器调试端口 {self.cdp_url}。\n\n"
                f"请先手动启动调试模式浏览器：\n"
                f'  "{self._find_chrome_exe()}" '
                f'--remote-debugging-port={self.debug_port} '
                f'--user-data-dir="{self.user_data_dir}"\n\n'
                f"或设置 auto_launch=True 让程序自动启动。"
            )

        logger.info("未检测到运行中的调试浏览器，通过 Playwright 启动...")
        self._launch_chrome_debug()

        # launch_persistent_context 返回 BrowserContext，直接用
        self._launched_context = self._launched_browser
        self._setup_page()

    def _setup_page(self):
        """连接/启动浏览器后设置页面引用。

        兼容两种返回类型:
          - CDP 连接 → self.browser 是 Browser 对象
          - launch_persistent_context → self._launched_context 是 BrowserContext 对象
        """
        if self._launched_context is not None:
            # launch_persistent_context 返回的是 BrowserContext
            context = self._launched_context
            pages = context.pages
            if pages:
                self.page = pages[0]
                logger.info(f"复用已有页面: {self.page.url}")
            else:
                self.page = context.new_page()
                logger.info("创建新页面")
        else:
            # CDP 连接返回的是 Browser
            contexts = self.browser.contexts
            if not contexts:
                self.page = self.browser.new_page()
                logger.info("创建新页面")
            else:
                context = contexts[0]
                pages = context.pages
                if pages:
                    self.page = pages[0]
                    logger.info(f"复用已有页面: {self.page.url}")
                else:
                    self.page = context.new_page()
                    logger.info("在已有 context 中创建新页面")

    # ── 登录 ────────────────────────────────────────────────────────────

    def login(self):
        """连接 Chrome 并确保京东已登录。

        登录态由 Chrome 的 user-data-dir 持久化。
        如果未登录，提示用户在浏览器中手动登录。
        """
        self._connect_browser()

        self.page.goto("https://www.jd.com/", wait_until="domcontentloaded")
        time.sleep(2)

        if self._check_login_status():
            logger.info("京东登录态有效")
            self._logged_in = True
            return

        logger.info("=" * 50)
        logger.info("⚠ 检测到京东未登录。请在 Chrome 窗口中完成登录。")
        logger.info("  （扫码或账号密码均可）")
        logger.info("  登录成功后程序将自动继续...")
        logger.info("=" * 50)

        start = time.time()
        while time.time() - start < 300:
            time.sleep(2)
            if self._check_login_status():
                logger.info("京东登录成功！")
                self._logged_in = True
                return

        raise RuntimeError("京东登录超时（5 分钟）。请在浏览器中完成登录后重试。")

    def _check_login_status(self) -> bool:
        """检查京东页面上的登录状态。"""
        try:
            logged_in_indicators = [
                'a:has-text("我的京东")',
                '[class*="nickname"]',
                'text=我的订单',
            ]
            for sel in logged_in_indicators:
                try:
                    if self.page.locator(sel).first.is_visible(timeout=2000):
                        return True
                except Exception:
                    continue

            cookies = self.page.context.cookies()
            for c in cookies:
                if c.get("name") in ("pin", "pt_pin", "pwdt_id"):
                    return True
            return False
        except Exception:
            return False

    # ── 搜索 + 加购 ─────────────────────────────────────────────────────

    def search_and_add(self, keyword: str) -> bool:
        """在京东搜索关键词并点击第一个有效的"加入购物车"按钮。

        流程:
          1. 确保在京东首页 → 填入搜索词 → 点击搜索
          2. 等待商品列表出现 → 额外等待按钮渲染
          3. 查找并点击加购按钮
          4. 若失败且 retry_on_failure=True → 刷新页面重试一次
        """
        if not self._logged_in:
            logger.error("请先调用 login() 确保京东已登录")
            return False

        search_term = f"{keyword} 自营"
        logger.info(f"搜索: {search_term}")

        def _do_search_and_click() -> bool:
            """执行一次完整的搜索+加购流程，返回 True/False。"""
            try:
                current_url = self.page.url
                if "search.jd.com" not in current_url and "jd.com" not in current_url:
                    self.page.goto("https://www.jd.com/", wait_until="domcontentloaded")
                    time.sleep(1)

                search_input = self._find_search_input()
                if search_input is None:
                    logger.error("  未找到京东搜索框")
                    return False

                search_input.click()
                time.sleep(0.2)
                search_input.fill("")
                time.sleep(0.1)
                search_input.fill(search_term)
                time.sleep(0.3)

                logger.info(f"  搜索关键词: {search_term}")
                search_btn = self._find_search_button()
                if search_btn:
                    search_btn.click()
                else:
                    search_input.press("Enter")

                # 等待商品列表出现
                logger.info("  等待商品列表出现...")
                try:
                    self.page.wait_for_selector(
                        '.gl-item, .J_goodsList, [class*="goods"]',
                        timeout=10000,
                    )
                    logger.info("  商品列表已加载")
                except Exception:
                    logger.warning("  等待商品列表超时")
                    try:
                        no_result = self.page.locator(
                            'text=抱歉没有找到, text=没有找到, text=无结果'
                        ).first
                        if no_result.is_visible(timeout=2000):
                            logger.warning(f"  京东无 '{keyword}' 的搜索结果")
                            return False
                    except Exception:
                        pass

                # 额外等待加购按钮渲染
                logger.info(f"  等待加购按钮渲染 ({self.search_wait_after}s)...")
                time.sleep(self.search_wait_after)

                logger.info("  查找'加入购物车'按钮...")
                return self._click_first_add_to_cart()

            except Exception as e:
                logger.error(f"  搜索加购异常: {e}")
                return False

        # 首次尝试
        success = _do_search_and_click()
        if success:
            logger.info(f"  ✓ '{keyword}' 加购成功")
            return True

        # 失败重试：刷新页面再试一次
        if self.retry_on_failure:
            logger.info(f"  首次加购失败，刷新页面重试...")
            try:
                self.page.reload(wait_until="domcontentloaded")
            except Exception:
                pass
            success = _do_search_and_click()
            if success:
                logger.info(f"  ✓ '{keyword}' 加购成功（重试后）")
                return True

        logger.warning(f"  ✗ '{keyword}' 加购失败")
        return False

    def _find_search_input(self):
        selectors = [
            'input#key',
            'input[name="keyword"]',
            'input[aria-label*="搜索" i]',
            'input[placeholder*="搜索" i]',
            'input.search-text',
            '#search input[type="text"]',
        ]
        for sel in selectors:
            try:
                el = self.page.locator(sel).first
                if el.is_visible(timeout=1000):
                    return el
            except Exception:
                continue
        return None

    def _find_search_button(self):
        selectors = [
            'button#search-btn',
            'button:has-text("搜索")',
            'input#search-btn[type="button"]',
            'a#search-btn',
            '[class*="search"] button',
        ]
        for sel in selectors:
            try:
                el = self.page.locator(sel).first
                if el.is_visible(timeout=1000):
                    return el
            except Exception:
                continue
        return None

    def _click_first_add_to_cart(self) -> bool:
        """在当前搜索结果页找到第一个有效的'加入购物车'按钮并点击。

        增强可靠性：
          - 先等待 networkidle 确保页面加载完毕
          - 滚动页面触发懒加载
          - 点击前 scroll_into_view + 输出 outerHTML 调试
          - 所有选择器失败时保存调试截图和 HTML
        """
        # 等待网络空闲，确保商品列表和加购按钮已渲染
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
            logger.info("  页面网络已空闲")
        except Exception:
            logger.info("  networkidle 超时，继续尝试查找按钮")

        # 滚动页面触发懒加载
        try:
            self.page.evaluate("window.scrollBy(0, 300)")
            time.sleep(0.5)
        except Exception:
            pass

        add_cart_selectors = [
            'button._addCart_65r2s_14',
            'button[class*="_addCart_"]',
            'a[href*="InitCartUrl"]',
            'a.btn-special',
            '.btn-special1',
            'a:has-text("加入购物车")',
            'button:has-text("加入购物车")',
            '[class*="cart-btn"]',
            '.add-cart-btn',
            '[data-type="add-cart"]',
        ]

        for sel in add_cart_selectors:
            try:
                elements = self.page.locator(sel)
                count = elements.count()
                logger.debug(f"  选择器 '{sel}' 匹配到 {count} 个元素")
                if count == 0:
                    continue

                for i in range(min(count, 5)):
                    el = elements.nth(i)

                    try:
                        el.wait_for(state="visible", timeout=3000)
                    except Exception:
                        continue

                    if not el.is_enabled():
                        continue

                    try:
                        el.scroll_into_view_if_needed()
                    except Exception:
                        pass

                    try:
                        html = el.evaluate("el => el.outerHTML")
                        logger.info(f"  找到加购按钮: {sel}\n    HTML: {html[:300]}")
                    except Exception:
                        logger.info(f"  找到加购按钮: {sel}")

                    for attempt in range(self.click_retry + 1):
                        try:
                            el.click(force=True, timeout=5000)
                        except Exception:
                            try:
                                el.click(timeout=5000)
                            except Exception:
                                pass

                        time.sleep(self.add_cart_delay)

                        if self._check_cart_success():
                            logger.info(f"  加购成功 (尝试 {attempt + 1})")
                            return True

                        if attempt < self.click_retry:
                            logger.info(
                                f"  未检测到成功提示，重试点击 "
                                f"({attempt + 2}/{self.click_retry + 1})"
                            )

                    logger.info("  点击完成")
                    return True

            except Exception:
                continue

        logger.info("  选择器未命中，尝试 JS 查找加购按钮...")
        try:
            self.page.evaluate("window.scrollBy(0, 400)")
            time.sleep(0.5)

            clicked = self.page.evaluate("""() => {
                const allButtons = document.querySelectorAll('button');
                for (const el of allButtons) {
                    const cls = el.className || '';
                    if (cls.includes('_addCart_') || cls.includes('addCart')) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            el.scrollIntoView({block: 'center'});
                            el.click();
                            return true;
                        }
                    }
                }
                const allElements = document.querySelectorAll('a, button');
                for (const el of allElements) {
                    const text = el.textContent || '';
                    if (text.includes('加入购物车') || text.includes('加入購物車')) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            el.scrollIntoView({block: 'center'});
                            el.click();
                            return true;
                        }
                    }
                }
                return false;
            }""")
            if clicked:
                time.sleep(self.add_cart_delay)
                if self._check_cart_success():
                    logger.info("  JS 加购成功")
                return True
        except Exception as e:
            logger.warning(f"  JS 查找加购按钮失败: {e}")

        # 全部失败：保存调试信息
        logger.warning("  未找到任何可用的'加入购物车'按钮，保存调试信息...")
        try:
            self.page.screenshot(path="temp/debug_no_add_button.png", full_page=False)
            logger.info("  调试截图已保存: debug_no_add_button.png")
        except Exception as e:
            logger.warning(f"  保存截图失败: {e}")
        try:
            with open("temp/debug_page.html", "w", encoding="utf-8") as f:
                f.write(self.page.content())
            logger.info("  调试页面源码已保存: debug_page.html")
        except Exception as e:
            logger.warning(f"  保存页面源码失败: {e}")

        return False

    def _check_cart_success(self) -> bool:
        """快速检查是否出现加购成功提示（短超时轮询）。"""
        success_selectors = [
            'text=已加入购物车',
            'text=加入成功',
            '#addcart-succ',
            '.add-cart-success',
        ]
        for sel in success_selectors:
            try:
                el = self.page.locator(sel).first
                if el.is_visible(timeout=500):
                    logger.info("  检测到加购成功提示")
                    return True
            except Exception:
                continue
        return False

    # ── 跳转购物车页 ────────────────────────────────────────────────────

    def open_cart_page(self):
        """跳转到京东购物车页面。"""
        logger.info("跳转到京东购物车页面...")
        self.page.goto("https://cart.jd.com/cart", wait_until="domcontentloaded")
        time.sleep(2)
        logger.info(f"当前页面: {self.page.url}")

    # ── 清理 ────────────────────────────────────────────────────────────

    def close(self):
        """断开连接，根据配置决定是否关闭浏览器进程。"""
        # 关闭程序创建的 page
        if self.page and not self.page.is_closed():
            try:
                self.page.close()
            except Exception:
                pass

        # 关闭 persistent context（由 launch_persistent_context 创建）
        if self._launched_context:
            try:
                self._launched_context.close()
            except Exception:
                pass

        # 断开 CDP / 关闭 Playwright 启动的浏览器
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass

        # 停止 Playwright
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass

        self.browser = None
        self.page = None
        self._pw = None
        self._launched_browser = None
        self._launched_context = None
        self._launched_by_us = False
        logger.info("连接已断开")
