"""FastAPI 服务:采集 API(127.0.0.1)+ Web 面板(Jinja2 模板)。"""
import hashlib
import json
import logging
import re
import threading
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from memvault import __version__
from memvault.config import chroma_dir, db_path, load_config, media_dir
from memvault.db import Database
from memvault.embeddings import get_text_embedder
from memvault.memory import Memory, fmt_ts
from memvault.vector_store import VectorStore

logger = logging.getLogger(__name__)
TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["ts"] = fmt_ts


def _from_json_attr(s):
    try:
        return list((json.loads(s) if s else {}).items())
    except (json.JSONDecodeError, TypeError):
        return []


templates.env.filters["from_json_attr"] = _from_json_attr

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")


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

    if start_worker:
        from memvault.worker import run_worker

        threading.Thread(target=run_worker, args=(memory, cfg),
                         daemon=True, name="memvault-worker").start()

    # ── 采集 API(浏览器插件 / 热键 / 其他工具)──────────────────────
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__,
                "items": memory.db.stats()["items"],
                "paused": app.state.paused}

    @app.post("/api/capture")
    def capture(req: CaptureReq):
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
        app.state.paused = not app.state.paused
        return {"paused": app.state.paused}

    # ── Web 面板 ──────────────────────────────────────────────────────
    def ctx(request: Request, **kw) -> dict:
        kw.setdefault("q", None)
        kw["request"] = request
        return kw

    @app.get("/")
    def index(request: Request):
        stats = memory.db.stats()
        items = memory.db.list_items(limit=50)
        return templates.TemplateResponse(request, "index.html",
            ctx(request, stats=stats, items=items))

    @app.get("/search")
    def search(request: Request, q: str = ""):
        results = memory.search(q, top_k=12) if q.strip() else []
        md = media_dir(cfg)
        for r in results:
            c = r["chunk"]
            c["jump"] = bili_jump(r["item"]["source_ref"], c.get("start_ts"))
            if c.get("media_path"):
                try:
                    c["media_url"] = "/media/" + str(
                        Path(c["media_path"]).relative_to(md)
                    ).replace("\\", "/")
                except ValueError:
                    c["media_url"] = None
        return templates.TemplateResponse(request, "search.html",
            ctx(request, results=results, q=q))

    @app.get("/inbox")
    def inbox(request: Request):
        items = memory.db.list_items(status="inbox", limit=100)
        return templates.TemplateResponse(request, "inbox.html",
            ctx(request, items=items))

    @app.post("/items/{item_id}/status")
    def set_status(item_id: int, status: str):
        if status not in ("inbox", "filed", "archived"):
            raise HTTPException(422, "非法 status")
        memory.db.set_item_status(item_id, status)
        return RedirectResponse("/inbox", status_code=303)

    @app.get("/items/{item_id}")
    def item_detail(request: Request, item_id: int):
        item = memory.get_item(item_id)
        if not item:
            raise HTTPException(404)
        md = media_dir(cfg)
        for c in item["chunks"]:
            c["jump"] = bili_jump(item["source_ref"], c.get("start_ts"))
            if c.get("media_path"):
                try:
                    c["media_url"] = "/media/" + str(
                        Path(c["media_path"]).relative_to(md)
                    ).replace("\\", "/")
                except ValueError:
                    c["media_url"] = None
        return templates.TemplateResponse(request, "item.html",
            ctx(request, item=item))

    @app.get("/jobs")
    def jobs(request: Request):
        rows = memory.db.list_jobs(50)
        return templates.TemplateResponse(request, "jobs.html",
            ctx(request, jobs=rows))

    # ── 分类管理(M3)────────────────────────────────────────────────
    @app.get("/categories")
    def categories_page(request: Request):
        from collections import defaultdict

        groups = defaultdict(list)
        for c in memory.db.categories():
            groups[c["domain"]].append(c)
        return templates.TemplateResponse(request, "categories.html",
            ctx(request, groups=dict(groups)))

    @app.post("/categories/add")
    def categories_add(domain: str, name: str):
        name = name.strip()[:40]
        if not name:
            raise HTTPException(422, "分类名不能为空")
        memory.db.add_category(domain or "general", name, status="active")
        return RedirectResponse("/categories", status_code=303)

    @app.post("/categories/{category_id}/confirm")
    def categories_confirm(category_id: int):
        memory.db.confirm_category(category_id)
        return RedirectResponse("/categories", status_code=303)

    @app.post("/items/{item_id}/reanalyze")
    def reanalyze(item_id: int):
        if not memory.get_item(item_id):
            raise HTTPException(404)
        memory.db.enqueue("auto_process", {"item_id": item_id})
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    return app
