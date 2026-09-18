"""FastAPI 服务:采集 API(127.0.0.1)+ Web 面板(Jinja2 模板)。"""
import hashlib
import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from fastapi import (FastAPI, File, Form, HTTPException, Request, UploadFile)
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from memvault import __version__
from memvault.config import (DEFAULT_CONFIG_PATH, chroma_dir, db_path,
                             load_config, media_dir)
from memvault.db import Database
from memvault.embeddings import get_text_embedder
from memvault.memory import Memory
from memvault.vector_store import VectorStore

logger = logging.getLogger(__name__)
TEMPLATES_DIR = Path(__file__).parent / "templates"
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024   # 以图搜图上传上限
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _from_json_attr(s):
    try:
        return list((json.loads(s) if s else {}).items())
    except (json.JSONDecodeError, TypeError):
        return []


templates.env.filters["from_json_attr"] = _from_json_attr

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def bili_jump(source_ref: str | None, start_ts) -> str | None:
    """B站来源 → 定位到时间戳的跳转链接。"""
    if not source_ref:
        return None
    m = BV_RE.search(source_ref)
    if not m:
        return None
    t = f"?t={int(start_ts)}" if start_ts is not None else ""
    return f"https://www.bilibili.com/video/{m.group(1)}{t}"


class CaptureReq(BaseModel):
    """采集协议,见 DESIGN §8.1。"""
    type: Literal["selection", "page", "product", "video"]
    url: Optional[str] = None
    title: Optional[str] = None
    text: Optional[str] = None
    domain: Optional[str] = None
    product: Optional[dict] = None
    video: Optional[dict] = None


class ChatReq(BaseModel):
    message: str
    skill: str = "general"


def create_app(cfg: dict | None = None, memory: Memory | None = None,
               start_worker: bool = False) -> FastAPI:
    cfg = cfg or load_config()
    if memory is None:
        db = Database(db_path(cfg))
        vs = VectorStore(chroma_dir(cfg))
        memory = Memory(db, vs, get_text_embedder(cfg))

    from memvault.prompts import PromptStore

    PromptStore(memory.db).ensure_seed()

    app = FastAPI(title="MemVault", version=__version__)
    app.state.cfg = cfg
    app.state.memory = memory
    app.state.paused = False

    app.mount("/media", StaticFiles(directory=str(media_dir(cfg))),
              name="media")
    app.mount("/static", StaticFiles(directory=str(TEMPLATES_DIR.parent / "static")),
              name="static")

    # JSON API 层(方案 A 第 0 步):前端/脚本的统一入口,与页面路由双轨并存
    from memvault.server.api import build_router, install_error_handlers

    install_error_handlers(app)
    app.include_router(build_router(memory, cfg))

    if start_worker:
        from memvault.scheduler import run_scheduler
        from memvault.worker import run_worker

        threading.Thread(target=run_worker, args=(memory, cfg),
                         daemon=True, name="memvault-worker").start()
        threading.Thread(target=run_scheduler, args=(memory, cfg),
                         daemon=True, name="memvault-scheduler").start()

    # ── 采集 API(浏览器插件 / 热键 / 其他工具)──────────────────────
    @app.get("/api/health")
    def health():
        """探活:插件/脚本用它判断服务是否在线。"""
        return {"ok": True, "version": __version__,
                "items": memory.db.stats()["items"],
                "paused": app.state.paused}

    @app.post("/api/capture")
    def capture(req: CaptureReq):
        """采集入口(见 DESIGN §8.1):selection/page/product/video。"""
        if app.state.paused:
            raise HTTPException(403, "采集已暂停")
        db = memory.db

        if req.type == "video":
            source = (req.video or {}).get("id") or req.url
            if not source:
                raise HTTPException(422, "video 采集需要 url 或 video.id")
            payload = {"source": source, "domain": req.domain or "general"}
            if req.video and req.video.get("t"):
                payload["start_ts"] = req.video["t"]
            jtype = "ingest_video"
            key = hashlib.sha1(("video|" + source).encode()).hexdigest()[:16]
        elif req.type in ("selection", "page"):
            text = (req.text or "").strip()
            if not text:
                raise HTTPException(422, "text 为空")
            payload = {"title": req.title, "url": req.url, "text": text,
                       "kind": req.type, "domain": req.domain or "general"}
            jtype = "ingest_text"
            key = hashlib.sha1((req.type + "|" + (req.url or "")
                                + "|" + text[:200]).encode()).hexdigest()[:16]
        elif req.type == "product":
            if not (req.product or {}).get("name") and not req.title:
                raise HTTPException(422, "product.name 或 title 必填")
            payload = {"url": req.url, "title": req.title,
                       "product": req.product or {},
                       "domain": req.domain or "shopping"}
            jtype = "ingest_product"
            key = hashlib.sha1(("product|" + (req.url or "")
                                + "|" + ((req.product or {}).get("name") or ""))
                               .encode()).hexdigest()[:16]
        else:  # pragma: no cover - Literal 已限
            raise HTTPException(422, "未知类型")

        job_id = db.enqueue(jtype, payload, dedup_key=key)
        return {"job_id": job_id, "status": "accepted"}

    @app.post("/api/pause")
    def pause_toggle():
        """暂停/恢复采集开关。"""
        app.state.paused = not app.state.paused
        return {"paused": app.state.paused}

    # ── Web 面板 ──────────────────────────────────────────────────────
    def ctx(request: Request, **kw) -> dict:
        kw.setdefault("q", None)
        kw["request"] = request
        return kw

    @app.get("/")
    def index(request: Request, domain: str = "", page: int = 1):
        """库首页(外壳;数据由 /api/items + /api/stats 提供)。"""
        # 本页已迁移到 Vue(方案 A):只渲染外壳,数据由 /api/stats 与
        # /api/items?with_related=1 提供;domain 透传进挂载点。
        return templates.TemplateResponse(request, "index.html",
            ctx(request, domain=domain))

    @app.get("/search")
    def search(request: Request, q: str = "", similar: str = ""):
        """检索页(外壳;数据由 /api/items + /api/search/images 提供)。"""
        # 本页已迁移到 Vue(方案 A):只渲染外壳,数据由 /api/items 与
        # /api/search/images 提供;q / similar 透传进挂载点(便于分享链接)。
        return templates.TemplateResponse(request, "search.html",
            ctx(request, q=q, similar=similar))

    @app.get("/inbox")
    def inbox(request: Request, domain: str = ""):
        """待整理箱(外壳;数据由 /api/inbox 提供)。"""
        # 本页已迁移到 Vue(方案 A):这里只渲染外壳,数据由 /api/inbox 提供。
        # 旧模板仍在 git 历史里,需要回退时 revert 对应提交即可。
        return templates.TemplateResponse(request, "inbox.html",
            ctx(request, domain=domain))

    @app.post("/items/{item_id}/classify")
    def item_classify(item_id: int, category_id: int = Form(...)):
        """手动把条目归入指定分类。"""
        cat = memory.db.get_category(category_id)
        if not cat or not memory.get_item(item_id):
            raise HTTPException(404, "分类或条目不存在")
        memory.db.set_item_category(item_id, category_id, None,
                                    f"手动归入:{cat['name']}")
        memory.db.set_item_status(item_id, "filed")
        return RedirectResponse("/inbox", status_code=303)

    @app.post("/inbox/batch")
    def inbox_batch(action: str = Form(...), domain: str = Form("")):
        """批量处理待整理箱:filed/archived 直接改状态,auto 交给 LLM 逐条分类。"""
        if action in ("filed", "archived"):
            n = memory.db.bulk_set_status("inbox", action, domain=domain or None)
            logger.info("批量处理待整理箱: %d 条 → %s", n, action)
        elif action == "auto":
            targets = memory.db.list_items(status="inbox", domain=domain or None,
                                           limit=500)
            for it in targets:
                memory.db.enqueue("auto_process", {"item_id": it["id"]})
            logger.info("批量交给 AI 分类: %d 条已入队", len(targets))
        else:
            raise HTTPException(422, "未知批量操作")
        return RedirectResponse("/inbox", status_code=303)

    @app.post("/items/{item_id}/status")
    def set_status(item_id: int, status: str):
        """旧表单:改状态后回待整理箱(revert 用,双轨保留)。"""
        if status not in ("inbox", "filed", "archived"):
            raise HTTPException(422, "非法 status")
        memory.db.set_item_status(item_id, status)
        return RedirectResponse("/inbox", status_code=303)

    @app.get("/items/{item_id}")
    def item_detail(request: Request, item_id: int):
        """条目详情页(外壳;数据由 /api/items/{id} 提供)。"""
        # 本页已迁移到 Vue(方案 A):只渲染外壳(标题留给 <title>),
        # 数据由 /api/items/{id} 提供。
        item = memory.get_item(item_id)
        if not item:
            raise HTTPException(404)
        return templates.TemplateResponse(request, "item.html",
            ctx(request, item=item))

    @app.get("/jobs")
    def jobs(request: Request):
        """任务页(外壳;数据由 /api/jobs 提供)。"""
        # 本页已迁移到 Vue(方案 A):只渲染外壳,数据由 /api/jobs 提供。
        return templates.TemplateResponse(request, "jobs.html", ctx(request))

    @app.get("/categories")
    def categories_page(request: Request):
        """分类管理页(外壳;数据由 /api/categories 提供)。"""
        # 本页已迁移到 Vue(方案 A):只渲染外壳,数据由 /api/categories 提供。
        return templates.TemplateResponse(request, "categories.html",
            ctx(request))

    @app.post("/categories/add")
    def categories_add(domain: str = Form(...), name: str = Form(...)):
        """旧表单:新增分类(双轨保留)。"""
        name = name.strip()[:40]
        if not name:
            raise HTTPException(422, "分类名不能为空")
        memory.db.add_category(domain or "general", name, status="active")
        return RedirectResponse("/categories", status_code=303)

    @app.post("/categories/{category_id}/confirm")
    def categories_confirm(category_id: int):
        """旧表单:采纳提议分类(双轨保留)。"""
        memory.db.confirm_category(category_id)
        return RedirectResponse("/categories", status_code=303)

    @app.post("/categories/{category_id}/delete")
    def categories_delete(category_id: int):
        """删除分类:条目退回待整理箱,专属提示词/UP规则一并清理。"""
        info = memory.db.delete_category(category_id)
        if info is None:
            raise HTTPException(404)
        logger.info("删除分类 %s/%s:条目退回 %d 条,清理提示词 %d 条、UP规则 %d 条",
                    info["domain"], info["name"], info["items"],
                    info["prompts"], info["up_rules"])
        return RedirectResponse("/categories", status_code=303)

    @app.post("/items/{item_id}/bind-up")
    def bind_up(item_id: int, category_id: str = Form(...), up_mid: str = Form(""),
                up_name: str = Form("")):
        """把这个 UP 的视频都归到该分类(可含本条:`filed` 直接归档)。"""
        item = memory.get_item(item_id)
        if not item:
            raise HTTPException(404)
        raw = json.loads(item.get("attrs_json") or "{}") or {}
        mid = up_mid or str(raw.get("up_mid") or "")
        if not mid:
            raise HTTPException(422, "该条目没有 UP主 信息")
        cat = memory.db.get_category(int(category_id))
        if not cat:
            raise HTTPException(422, "分类不存在")
        name = up_name or raw.get("up")
        memory.db.bind_up_category(mid, name, cat["id"])
        memory.db.set_item_category(item_id, cat["id"], 1.0,
                                    f"UP主规则:{name or mid} 的视频归入本分类")
        memory.db.set_item_status(item_id, "filed")
        logger.info("UP主规则已绑定:%s(%s) → 分类 %s", name, mid, cat["name"])
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.post("/up/{up_mid}/unbind")
    def unbind_up(up_mid: str, back: str = Form("categories")):
        """旧表单:解除 UP 规则(双轨保留)。"""
        memory.db.unbind_up_category(up_mid)
        target = "/categories" if back == "categories" else "/"
        return RedirectResponse(target, status_code=303)

    @app.post("/items/{item_id}/reanalyze")
    def reanalyze(item_id: int):
        """旧表单:重新分析(双轨保留)。"""
        if not memory.get_item(item_id):
            raise HTTPException(404)
        memory.db.enqueue("auto_process", {"item_id": item_id})
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.post("/items/{item_id}/cart")
    def item_cart(item_id: int):
        """把库中条目加入京东购物车(用户显式点击,worker 异步执行)。"""
        item = memory.get_item(item_id)
        if not item:
            raise HTTPException(404)
        memory.db.enqueue("jd_cart", {"keyword": item["title"],
                                      "item_id": item_id})
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    # ── 聊天(M3b:对话式画像/日志录入 + 问答)──────────────────────
    from memvault.llm import get_llm_client

    app.state.llm = get_llm_client(cfg)

    @app.get("/chat")
    def chat_page(request: Request):
        """聊天页(外壳;对话走 POST /api/chat)。"""
        return templates.TemplateResponse(request, "chat.html", ctx(request))

    # ── 订阅管理(M3b)────────────────────────────────────────────────
    @app.get("/sources")
    def sources_page(request: Request):
        """订阅页(外壳;数据由 /api/sources 提供)。"""
        # 本页已迁移到 Vue(方案 A):只渲染外壳,数据由 /api/sources 提供。
        return templates.TemplateResponse(request, "sources.html", ctx(request))

    @app.post("/sources/add")
    def sources_add(kind: str = Form(...), target: str = Form(...),
                    domain: str = Form("general")):
        """旧表单:新增订阅(双轨保留)。"""
        from memvault.sources import bili_watch

        mid = bili_watch.parse_mid(target)
        if not mid:
            raise HTTPException(422, "无法解析 UP主 UID(需 UID 或 space 链接)")
        label = bili_watch.up_name(mid)
        memory.db.add_watch_source(kind, mid,
                                   domain=domain or "general", label=label)
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/toggle")
    def sources_toggle(source_id: int):
        """旧表单:启停订阅(双轨保留)。"""
        if not memory.db.get_watch_source(source_id):
            raise HTTPException(404)
        memory.db.toggle_watch_source(source_id)
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/check")
    def sources_check(source_id: int):
        """旧表单:立即检查订阅(双轨保留)。"""
        if not memory.db.get_watch_source(source_id):
            raise HTTPException(404)
        memory.db.enqueue("watch_check", {"source_id": source_id})
        return RedirectResponse("/sources", status_code=303)

    # ── 设置:API 配置(在线切换,免手改文件)────────────────────────
    from memvault import llm as llm_mod
    from urllib.parse import quote as _quote

    @app.get("/settings")
    def settings_page(request: Request):
        """设置页(外壳;数据由 /api/settings 提供)。"""
        # 本页已迁移到 Vue(方案 A):只渲染外壳,数据由 /api/settings 提供
        # (密钥只回显"是否已配置",不回明文)。
        return templates.TemplateResponse(request, "settings.html", ctx(request))

    @app.post("/settings/save")
    def settings_save(backend: str = Form("api"),
                      base_url: str = Form(...), model: str = Form(...),
                      api_key: str = Form(""),
                      classify_confidence: float = Form(0.8)):
        """写回 config.yaml(后端/base_url/model/阈值)与 .env(密钥),即时生效。"""
        import yaml as _yaml
        p = Path(cfg.get("_config_path") or DEFAULT_CONFIG_PATH)
        raw = {}
        if p.exists():
            raw = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        sec = raw.setdefault("llm", {})
        sec["backend"] = backend if backend in ("api", "web") else "api"
        sec["base_url"] = base_url.strip()
        sec["model"] = model.strip()
        sec["classify_confidence"] = classify_confidence
        p.write_text(_yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                     encoding="utf-8")

        cfg["llm"]["backend"] = sec["backend"]
        cfg["llm"]["base_url"] = base_url.strip()
        cfg["llm"]["model"] = model.strip()
        cfg["llm"]["classify_confidence"] = classify_confidence
        if api_key.strip():
            env = llm_mod.PROJECT_ROOT / ".env"
            lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
            lines = [x for x in lines if not x.startswith("DEEPSEEK_API_KEY=")]
            lines.append(f"DEEPSEEK_API_KEY={api_key.strip()}")
            env.write_text("\n".join(lines) + "\n", encoding="utf-8")
            cfg["llm"]["api_key"] = api_key.strip()

        llm_mod.reset_llm_client()  # 切换后端时关闭旧的浏览器实例
        app.state.llm = llm_mod.get_llm_client(cfg)
        logger.info("LLM 设置已保存: backend=%s model=%s",
                    sec["backend"], cfg["llm"]["model"])
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/test")
    def settings_test():
        """旧表单:测试 LLM 连接(双轨保留)。"""
        client = app.state.llm
        if not getattr(client, "enabled", False):
            return RedirectResponse("/settings?test=" + _quote("未配置 API key"),
                                    status_code=303)
        try:
            # 预算给足:推理型模型的思考 token 会占用 max_tokens
            client.chat_json("你是回声机,只输出 JSON。", '回复 {"ok": true}',
                             max_tokens=800)
            msg = f"连接成功 ✓ 当前模型:{client.model}"
        except Exception as e:  # noqa: BLE001
            msg = f"连接失败 ✗ {str(e)[:120]}"
        return RedirectResponse("/settings?test=" + _quote(msg), status_code=303)

    return app
