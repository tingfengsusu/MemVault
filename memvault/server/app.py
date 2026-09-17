"""FastAPI 服务:采集 API(127.0.0.1)+ Web 面板(Jinja2 模板)。"""
import hashlib
import json
import logging
import re
import threading
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from memvault import __version__
from memvault.config import (DEFAULT_CONFIG_PATH, chroma_dir, db_path,
                             load_config, media_dir)
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
    def index(request: Request, domain: str = "", page: int = 1):
        page = max(1, page)
        stats = memory.db.stats()
        items = memory.db.list_items(domain=domain or None,
                                     limit=50, offset=(page - 1) * 50)
        return templates.TemplateResponse(request, "index.html",
            ctx(request, stats=stats, items=items, domain=domain, page=page,
                domain_counts=memory.db.items_by_domain()))

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
    def inbox(request: Request, domain: str = ""):
        from collections import defaultdict as _dd

        items = memory.db.list_items(status="inbox", domain=domain or None,
                                     limit=200)
        all_cats = memory.db.categories()
        cats_by_domain = _dd(list)
        for c in all_cats:
            if c.get("status") != "archived":
                cats_by_domain[c["domain"]].append(c)
        cat_names = {c["id"]: c["name"] for c in all_cats}
        return templates.TemplateResponse(request, "inbox.html",
            ctx(request, items=items, domain=domain,
                domain_counts=memory.db.items_by_domain(status="inbox"),
                cats_by_domain=dict(cats_by_domain), cat_names=cat_names))

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
        if status not in ("inbox", "filed", "archived"):
            raise HTTPException(422, "非法 status")
        memory.db.set_item_status(item_id, status)
        return RedirectResponse("/inbox", status_code=303)

    @app.get("/items/{item_id}")
    def item_detail(request: Request, item_id: int):
        item = memory.get_item(item_id)
        if not item:
            raise HTTPException(404)
        cat = memory.db.get_category(item["category_id"]) \
            if item.get("category_id") else None
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
            ctx(request, item=item, category_name=cat["name"] if cat else None))

    @app.get("/jobs")
    def jobs(request: Request):
        from datetime import datetime as _dt

        rows = memory.db.list_jobs(50)
        for j in rows:
            j["duration"] = ""
            if j.get("started_at") and j.get("finished_at"):
                try:
                    s = _dt.strptime(j["started_at"], "%Y-%m-%d %H:%M:%S")
                    f = _dt.strptime(j["finished_at"], "%Y-%m-%d %H:%M:%S")
                    sec = (f - s).total_seconds()
                    j["duration"] = f"{sec:.0f} 秒" if sec < 120 else f"{sec / 60:.1f} 分钟"
                except (ValueError, TypeError):
                    pass
            try:  # 参数摘要:解析 JSON 取关键信息
                p = json.loads(j["payload"])
                j["payload_pretty"] = " · ".join(
                    f"{k}={str(v)[:70]}" for k, v in p.items()) or "(空)"
            except (json.JSONDecodeError, AttributeError):
                j["payload_pretty"] = j["payload"][:120]
        return templates.TemplateResponse(request, "jobs.html",
            ctx(request, jobs=rows))

    # ── 分类管理(M3)────────────────────────────────────────────────
    @app.get("/categories")
    def categories_page(request: Request):
        from collections import defaultdict

        groups = defaultdict(list)
        for c in memory.db.categories():
            c["item_count"] = memory.db.count_items(category_id=c["id"])
            groups[c["domain"]].append(c)
        return templates.TemplateResponse(request, "categories.html",
            ctx(request, groups=dict(groups)))

    @app.post("/categories/add")
    def categories_add(domain: str = Form(...), name: str = Form(...)):
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
    from memvault.llm import LLMClient

    app.state.llm = LLMClient(cfg)

    @app.get("/chat")
    def chat_page(request: Request):
        return templates.TemplateResponse(request, "chat.html", ctx(request))

    @app.post("/api/chat")
    def api_chat(req: ChatReq):
        llm = app.state.llm
        if not llm.enabled:
            raise HTTPException(503, "LLM 未配置(设置 DEEPSEEK_API_KEY 后重启)")
        from memvault.chat import chat_turn

        try:
            return chat_turn(memory, llm, req.message, req.skill or "general")
        except ValueError as e:
            raise HTTPException(422, str(e))
        except Exception as e:  # noqa: BLE001 — 上游 API 异常转 502
            logger.exception("聊天处理失败")
            raise HTTPException(502, f"LLM 调用失败: {e}")

    # ── 订阅管理(M3b)────────────────────────────────────────────────
    @app.get("/sources")
    def sources_page(request: Request):
        return templates.TemplateResponse(request, "sources.html",
            ctx(request, sources=memory.db.watch_sources()))

    @app.post("/sources/add")
    def sources_add(kind: str = Form(...), target: str = Form(...),
                    domain: str = Form("general")):
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
        if not memory.db.get_watch_source(source_id):
            raise HTTPException(404)
        memory.db.toggle_watch_source(source_id)
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/check")
    def sources_check(source_id: int):
        if not memory.db.get_watch_source(source_id):
            raise HTTPException(404)
        memory.db.enqueue("watch_check", {"source_id": source_id})
        return RedirectResponse("/sources", status_code=303)

    # ── 设置:API 配置(在线切换,免手改文件)────────────────────────
    from memvault import llm as llm_mod
    from urllib.parse import quote as _quote

    @app.get("/settings")
    def settings_page(request: Request, saved: int = 0, test: str = ""):
        l = cfg.get("llm", {})
        key = app.state.llm.api_key or ""
        if len(key) > 12:
            masked = key[:6] + "…" + key[-4:]
        elif key:
            masked = "已配置"
        else:
            masked = "未配置"
        return templates.TemplateResponse(request, "settings.html",
            ctx(request, llm=l, key_masked=masked, saved=bool(saved),
                test_result=test, asr=cfg.get("asr", {}),
                emb=cfg.get("embedding", {})))

    @app.post("/settings/save")
    def settings_save(base_url: str = Form(...), model: str = Form(...),
                      api_key: str = Form(""),
                      classify_confidence: float = Form(0.8)):
        """写回 config.yaml(base_url/model/阈值)与 .env(密钥),即时生效。"""
        import yaml as _yaml
        p = Path(cfg.get("_config_path") or DEFAULT_CONFIG_PATH)
        raw = {}
        if p.exists():
            raw = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        sec = raw.setdefault("llm", {})
        sec["base_url"] = base_url.strip()
        sec["model"] = model.strip()
        sec["classify_confidence"] = classify_confidence
        p.write_text(_yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                     encoding="utf-8")

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

        app.state.llm = llm_mod.LLMClient(cfg)  # 即时重建客户端
        logger.info("API 设置已保存并生效: model=%s", cfg["llm"]["model"])
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/test")
    def settings_test():
        client = app.state.llm
        if not client.enabled:
            return RedirectResponse("/settings?test=" + _quote("未配置 API key"),
                                    status_code=303)
        try:
            client.chat_json("你是回声机,只输出 JSON。", '回复 {"ok": true}',
                             max_tokens=50)
            msg = f"连接成功 ✓ 当前模型:{client.model}"
        except Exception as e:  # noqa: BLE001
            msg = f"连接失败 ✗ {str(e)[:100]}"
        return RedirectResponse("/settings?test=" + _quote(msg), status_code=303)

    return app
