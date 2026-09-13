"""托盘常驻程序:uvicorn 线程 + worker 线程 + 全局热键 + 托盘菜单。

运行:python -m memvault tray(或仓库根目录 run_tray.py,配合计划任务自启)。
"""
import logging
import threading
import time
import webbrowser

from memvault.config import chroma_dir, db_path, load_config
from memvault.db import Database
from memvault.embeddings import get_text_embedder
from memvault.memory import Memory
from memvault.vector_store import VectorStore

logger = logging.getLogger(__name__)


def panel_url(cfg: dict) -> str:
    s = cfg["server"]
    return f"http://{s['host']}:{s['port']}"


def already_running(cfg: dict) -> bool:
    import httpx

    try:
        r = httpx.get(panel_url(cfg) + "/api/health", timeout=1.5)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def _make_icon_image():
    """程序化生成托盘图标(深底圆角方块 + M)。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=14, fill="#1f2937")
    d.line([(20, 20), (20, 44)], fill="#ffffff", width=6)
    d.line([(20, 22), (32, 34)], fill="#a5b4fc", width=6)
    d.line([(32, 34), (44, 22)], fill="#a5b4fc", width=6)
    d.line([(44, 20), (44, 44)], fill="#ffffff", width=6)
    return img


def run(cfg: dict | None = None):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    cfg = cfg or load_config()

    if already_running(cfg):
        logger.info("MemVault 已在运行,直接打开面板")
        webbrowser.open(panel_url(cfg))
        return

    try:
        import pystray
    except ImportError:
        logger.error("缺少托盘依赖:pip install -r requirements-m2.txt")
        return

    # 组装服务(不依赖 FastAPI app 的注入路径,独立构建)
    db = Database(db_path(cfg))
    vs = VectorStore(chroma_dir(cfg))
    memory = Memory(db, vs, get_text_embedder(cfg))

    from memvault.server.app import create_app
    from memvault.worker import run_worker

    app = create_app(cfg, memory=memory)

    import uvicorn

    s = cfg["server"]
    uconfig = uvicorn.Config(app, host=s["host"], port=s["port"],
                             log_level="warning")
    server = uvicorn.Server(uconfig)
    threading.Thread(target=server.run, daemon=True,
                     name="memvault-server").start()

    stop_event = threading.Event()
    threading.Thread(target=run_worker, args=(memory, cfg),
                     kwargs={"stop": stop_event.is_set, "poll_seconds": 2.0},
                     daemon=True, name="memvault-worker").start()

    from memvault.scheduler import run_scheduler

    threading.Thread(target=run_scheduler, args=(memory, cfg),
                     kwargs={"stop": stop_event.is_set},
                     daemon=True, name="memvault-scheduler").start()

    # 等服务就绪
    for _ in range(40):
        if already_running(cfg):
            break
        time.sleep(0.5)

    # 全局热键:剪贴板 → 采集
    hcfg = cfg.get("hotkey", {})
    if hcfg.get("enabled", True):
        try:
            import keyboard

            from memvault.pipeline.files import capture_from_clipboard

            def on_hotkey():
                result = capture_from_clipboard(memory, cfg)
                level = logging.INFO if result.get("ok") else logging.WARNING
                logger.log(level, "热键采集 [%s] %s", result.get("kind"),
                           result.get("detail"))

            keyboard.add_hotkey(hcfg.get("keys", "ctrl+alt+b"), on_hotkey,
                                suppress=False)
            logger.info("全局热键已注册:%s", hcfg.get("keys", "ctrl+alt+b"))
        except Exception as e:  # noqa: BLE001 — 热键失败不阻止常驻
            logger.warning("热键注册失败(可关闭 config.hotkey.enabled):%s", e)

    # 托盘菜单
    def open_panel(_icon, _item):
        webbrowser.open(panel_url(cfg))

    def open_inbox(_icon, _item):
        webbrowser.open(panel_url(cfg) + "/inbox")

    def toggle_pause(_icon, _item):
        import httpx

        try:
            r = httpx.post(panel_url(cfg) + "/api/pause", timeout=3)
            logger.info("采集暂停状态:%s", r.json().get("paused"))
        except httpx.HTTPError as e:
            logger.warning("切换暂停失败:%s", e)

    def quit_app(_icon, _item):
        stop_event.set()
        server.should_exit = True
        _icon.stop()

    def inbox_label(item):
        try:
            n = db.stats()["inbox"]
        except Exception:  # noqa: BLE001 — 面板计数失败不致命
            n = "?"
        return f"待整理箱({n})"

    menu = pystray.Menu(
        pystray.MenuItem("打开面板", open_panel, default=True),
        pystray.MenuItem(inbox_label, open_inbox),
        pystray.MenuItem("暂停/恢复采集", toggle_pause),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )
    icon = pystray.Icon("MemVault", _make_icon_image(),
                        "MemVault — 个人记忆库", menu)

    def refresh_menu():
        while not stop_event.is_set():
            time.sleep(15)
            try:
                icon.update_menu()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=refresh_menu, daemon=True).start()

    logger.info("MemVault 常驻启动完成,面板:%s", panel_url(cfg))
    icon.run()  # 阻塞主线程,退出菜单后返回
    logger.info("MemVault 已退出")


if __name__ == "__main__":
    run()
