"""JSON API 层(方案 A 第 0 步):面板前端(Vue)与脚本/外部调用的统一数据接口。

契约(前端只需实现一次解包逻辑):
    成功 {"ok": true,  "data": ..., "error": null}
    失败 {"ok": false, "data": null, "error": {"code": "...", "message": "..."}}
分页统一 {"items": [...], "total": n, "page": p, "page_size": s}。

设计约束:
- **只加不改**:旧页面路由(Jinja2 + redirect)完全不动,双轨并存,随时可回退;
- 业务异常统一由 install_error_handlers 映射成上面的失败形状,路由里只抛 HTTPException;
- 序列化只做"视图整形"(解析 attrs/media_paths、补缩略图与跳转链接),不写业务逻辑。
"""
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import (APIRouter, Body, File, HTTPException, Request, UploadFile)
from fastapi.exception_handlers import http_exception_handler


logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


# ── 响应契约 ──────────────────────────────────────────────────────────
def _bili_jump(source_ref, start_ts):
    """B站时间戳跳转链接(app.py 同名函数的副本,避免循环导入)。"""
    if not source_ref or start_ts is None:
        return None
    m = __import__("re").search(r"(BV[0-9A-Za-z]+)", str(source_ref))
    if not m:
        return None
    return f"https://www.bilibili.com/video/{m.group(1)}?t={int(float(start_ts))}"


def ok(data):
    return {"ok": True, "data": data, "error": None}


def page_payload(items, total: int, page: int, page_size: int, **extra):
    return ok({"items": items, "total": total, "page": page,
               "page_size": page_size, **extra})


def fail(code: str, message: str, status: int = 400):
    raise HTTPException(status_code=status,
                        detail={"code": code, "message": message})


def install_error_handlers(app):
    """把业务异常渲染成统一契约;**仅对 /api 路径生效**(页面路由保持原生行为)。"""

    @app.exception_handler(HTTPException)
    async def _api_http_error(request: Request, exc: HTTPException):
        if not request.url.path.startswith("/api"):
            return await http_exception_handler(request, exc)
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            err = {"code": detail["code"], "message": detail.get("message", "")}
        else:
            err = {"code": f"http_{exc.status_code}", "message": str(detail)}
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code,
                            content={"ok": False, "data": None, "error": err})

    @app.exception_handler(Exception)
    async def _api_unexpected(request: Request, exc: Exception):
        if not request.url.path.startswith("/api"):
            raise exc
        logger.exception("API 未预期错误: %s", request.url.path)
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=500, content={
            "ok": False, "data": None,
            "error": {"code": "internal_error", "message": str(exc)}})


# ── 视图整形 ──────────────────────────────────────────────────────────
def _json_or(value, fallback):
    if not value:
        return fallback
    try:
        out = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return out if isinstance(out, type(fallback)) else fallback


def _media_url(cfg: dict, path) -> Optional[str]:
    """本地媒体文件 → /media 下的相对 URL(仅允许媒体目录内的文件)。"""
    if not path:
        return None
    from memvault.config import media_dir

    try:
        rel = Path(path).relative_to(media_dir(cfg))
    except ValueError:
        return None
    return "/media/" + str(rel).replace("\\", "/")


class Serializer:
    """条目/分类/任务等对象的 JSON 视图(面板不同页面复用同一形状)。"""

    def __init__(self, memory, cfg: dict):
        self.memory = memory
        self.cfg = cfg

    def item_brief(self, it: dict) -> dict:
        media = _json_or(it.get("media_paths"), [])
        cat = (self.memory.db.get_category(it["category_id"])
               if it.get("category_id") else None)
        return {
            "id": it["id"],
            "title": it["title"],
            "domain": it["domain"],
            "type": it["type"],
            "status": it["status"],
            "category_id": it.get("category_id"),
            "category_name": cat["name"] if cat else None,
            "chunk_count": it.get("chunk_count"),
            "snippet": (it.get("content_text") or "")[:150],
            "thumb": _media_url(self.cfg, media[0]) if media else None,
            "source_type": it.get("source_type"),
            "source_ref": it.get("source_ref"),
            "created_at": it.get("created_at"),
        }

    def item_detail(self, it: dict) -> dict:
        from memvault.memory import fmt_ts

        out = self.item_brief(it)
        out["chunk_count"] = len(it.get("chunks", []))
        out["attrs_ai"] = _json_or(it.get("attrs_ai"), {})
        out["attrs_raw"] = _json_or(it.get("attrs_json"), {})
        out["auto_note"] = it.get("auto_note")
        out["category_conf"] = it.get("category_conf")
        out["content_text"] = it.get("content_text")
        raw = out["attrs_raw"]
        up_mid = raw.get("up_mid")
        rule = self.memory.db.up_category(up_mid) if up_mid else None
        out["up"] = {"up_mid": up_mid, "up_name": raw.get("up"),
                     "rule_category_id": rule["category_id"] if rule else None}
        related = self.memory.db.links_for_items([it["id"]]).get(it["id"], [])
        out["related"] = related
        # 关键片段(购物稿 ②):边界已吸附到结构单元,面板按时间轴展示
        from memvault.memory import fmt_ts
        clips = []
        for c in self.memory.db.clips_for_item(it["id"]):
            clips.append({
                "id": c["id"], "kind": c["kind"], "reason": c.get("reason"),
                "confidence": c.get("confidence"),
                "start_ts": c.get("start_ts"), "end_ts": c.get("end_ts"),
                "start_label": (fmt_ts(c["start_ts"])
                                if c.get("start_ts") is not None else ""),
                "end_label": (fmt_ts(c["end_ts"])
                              if c.get("end_ts") is not None else ""),
                "jump": _bili_jump(it.get("source_ref"), c.get("start_ts")),
            })
        out["clips"] = clips
        chunks = []
        for c in it.get("chunks", []):
            chunks.append({
                "id": c["id"],
                "modality": c["modality"],
                "content": c.get("content"),
                "start_ts": c.get("start_ts"),
                "end_ts": c.get("end_ts"),
                "seq": c.get("seq"),
                "embed_status": c.get("embed_status"),
                "timestamp_label": (fmt_ts(c["start_ts"])
                                    if c.get("start_ts") is not None else None),
                "media_url": _media_url(self.cfg, c.get("media_path")),
                "jump": self._jump(it.get("source_ref"), c.get("start_ts")),
                "source_ref": it.get("source_ref"),
            })
        out["chunks"] = chunks
        return out

    @staticmethod
    def _jump(source_ref, start_ts):
        """B站来源 → 定位到时间戳的链接(与页面路由同一实现)。"""
        from memvault.server.app import bili_jump

        return bili_jump(source_ref, start_ts)

    def image_hit(self, h: dict) -> dict:
        c, it = h["chunk"], h["item"]
        return {
            "score": h["score"],
            "chunk_id": c["id"],
            "start_ts": c.get("start_ts"),
            "media_url": _media_url(self.cfg, c.get("media_path")),
            "item": {"id": it["id"], "title": it["title"],
                     "domain": it["domain"], "type": it["type"]},
        }

    def category(self, c: dict) -> dict:
        return {
            "id": c["id"], "domain": c["domain"], "name": c["name"],
            "status": c.get("status"), "parent_id": c.get("parent_id"),
            "item_count": self.memory.db.count_items(category_id=c["id"]),
        }

    def job(self, j: dict) -> dict:
        def _pretty(payload: str) -> str:
            try:
                p = json.loads(payload)
            except (TypeError, ValueError):
                return (payload or "")[:120]
            return " · ".join(f"{k}={str(v)[:70]}" for k, v in p.items()) or "(空)"

        return {
            "id": j["id"], "type": j["type"], "status": j["status"],
            "error": j.get("error"), "created_at": j.get("created_at"),
            "started_at": j.get("started_at"), "finished_at": j.get("finished_at"),
            "duration": self._duration(j.get("started_at"), j.get("finished_at")),
            "payload": j.get("payload"), "payload_pretty": _pretty(j.get("payload")),
        }

    @staticmethod
    def _duration(started, finished) -> str:
        """耗时的人类可读形式(与任务页原来一致)。"""
        if not started or not finished:
            return ""
        from datetime import datetime as _dt

        try:
            s = _dt.strptime(started, "%Y-%m-%d %H:%M:%S")
            f = _dt.strptime(finished, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return ""
        sec = (f - s).total_seconds()
        return f"{sec:.0f} 秒" if sec < 120 else f"{sec / 60:.1f} 分钟"

    def source(self, s: dict) -> dict:
        return {k: s.get(k) for k in
                ("id", "kind", "target", "domain", "enabled", "label",
                 "last_checked", "created_at")}


# ── 路由 ─────────────────────────────────────────────────────────────
def build_router(memory, cfg: dict) -> APIRouter:
    """构造 /api 路由(与页面路由共用同一个 memory 实例)。"""
    api = APIRouter(prefix="/api", tags=["api"])
    ser = Serializer(memory, cfg)

    def _page_args(page: int, page_size: int) -> tuple[int, int]:
        page = max(1, int(page or 1))
        page_size = min(MAX_PAGE_SIZE, max(1, int(page_size or DEFAULT_PAGE_SIZE)))
        return page, page_size

    @api.get("/items")
    def list_items(q: str = "", domain: str = "", status: str = "",
                   category_id: Optional[int] = None, with_related: int = 0,
                   page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
        """检索条目:q 走混合检索(向量+关键词),否则按 domain/status 过滤分页。

        with_related=1 时给每条带上"相关条目"(库首页卡片用),一次批量查询。
        """
        page, page_size = _page_args(page, page_size)
        if q.strip():
            # 混合检索没有 SQL 分页语义 → 取前 page*page_size 条再切片,
            # total 是"检索能给出的上限"(本地单用户足够用)
            hits = memory.search(q, domain=domain or None, top_k=page * page_size)
            total = len(hits)
            start = (page - 1) * page_size
            data = []
            for h in hits[start:start + page_size]:
                it, c = h["item"], h["chunk"]
                brief = ser.item_brief(it)
                brief["hit_chunk"] = {
                    "id": c["id"],
                    "content": (c.get("content") or "")[:300],
                    "start_ts": c.get("start_ts"),
                    "media_url": _media_url(cfg, c.get("media_path")),
                    "source_ref": it.get("source_ref"),
                }
                data.append(brief)
            return page_payload(data, total, page, page_size, query=q)
        limit, offset = page_size, (page - 1) * page_size
        rows = memory.db.list_items(status=status or None,
                                    domain=domain or None,
                                    limit=limit, offset=offset)
        total = memory.db.count_items(status=status or None,
                                      domain=domain or None,
                                      category_id=category_id)
        items = [ser.item_brief(it) for it in rows]
        if category_id:   # list_items 没有分类过滤,这里补一层
            items = [it for it in items if it["category_id"] == category_id]
        if with_related and items:
            rel = memory.db.links_for_items([it["id"] for it in items])
            for it in items:
                it["related"] = rel.get(it["id"], [])
        return page_payload(items, total, page, page_size)

    @api.get("/items/{item_id}")
    def item_detail(item_id: int):
        """条目详情:语义块(带时间戳/B站跳转)、AI 属性、相关条目、UP 信息。"""
        it = memory.get_item(item_id)
        if not it:
            fail("not_found", f"条目不存在: {item_id}", 404)
        return ok(ser.item_detail(it))

    @api.post("/items/{item_id}/status")
    def set_item_status(item_id: int, status: str = Body(..., embed=True)):
        """改条目状态:inbox / filed / archived。"""
        if status not in ("inbox", "filed", "archived"):
            fail("invalid_status", f"非法 status: {status}", 422)
        if not memory.get_item(item_id):
            fail("not_found", f"条目不存在: {item_id}", 404)
        memory.db.set_item_status(item_id, status)
        return ok(ser.item_brief(memory.get_item(item_id)))

    @api.post("/items/{item_id}/reanalyze")
    def reanalyze(item_id: int):
        """重新分析(交给队列:路由 + 提取)。"""
        if not memory.get_item(item_id):
            fail("not_found", f"条目不存在: {item_id}", 404)
        memory.db.enqueue("auto_process", {"item_id": item_id})
        return ok({"item_id": item_id, "queued": "auto_process"})

    @api.post("/items/{item_id}/classify")
    def classify(item_id: int, category_id: int = Body(..., embed=True)):
        """手动归入指定分类(待整理箱的"归入所选分类")。"""
        it = memory.get_item(item_id)
        if not it:
            fail("not_found", f"条目不存在: {item_id}", 404)
        cat = memory.db.get_category(int(category_id))
        if not cat:
            fail("not_found", "分类不存在", 404)
        memory.db.set_item_category(item_id, cat["id"], None,
                                    f"手动归入:{cat['name']}")
        memory.db.set_item_status(item_id, "filed")
        return ok(ser.item_brief(memory.get_item(item_id)))

    @api.post("/items/{item_id}/cart")
    def item_cart(item_id: int):
        """把条目加入京东购物车(用户显式点击,worker 异步执行)。"""
        it = memory.get_item(item_id)
        if not it:
            fail("not_found", f"条目不存在: {item_id}", 404)
        memory.db.enqueue("jd_cart", {"keyword": it["title"], "item_id": item_id})
        return ok({"item_id": item_id, "queued": "jd_cart"})

    @api.post("/items/{item_id}/bind-up")
    def bind_up(item_id: int, category_id: int = Body(...),
                up_mid: str = Body(""), up_name: str = Body("")):
        """把这个 UP 的视频都归到该分类(可含本条:直接归档)。"""
        it = memory.get_item(item_id)
        if not it:
            fail("not_found", f"条目不存在: {item_id}", 404)
        raw = _json_or(it.get("attrs_json"), {})
        mid = str(up_mid or raw.get("up_mid") or "")
        if not mid:
            fail("no_up", "该条目没有 UP主 信息", 422)
        cat = memory.db.get_category(int(category_id))
        if not cat:
            fail("not_found", "分类不存在", 404)
        name = up_name or raw.get("up")
        memory.db.bind_up_category(mid, name, cat["id"])
        memory.db.set_item_category(item_id, cat["id"], 1.0,
                                    f"UP主规则:{name or mid} 的视频归入本分类")
        memory.db.set_item_status(item_id, "filed")
        return ok({"up_mid": mid, "category_id": cat["id"],
                   "category_name": cat["name"],
                   "item": ser.item_brief(memory.get_item(item_id))})

    @api.post("/up/{up_mid}/unbind")
    def unbind_up(up_mid: str):
        """解除某个 UP 的分类规则。"""
        n = memory.db.unbind_up_category(up_mid)
        return ok({"up_mid": up_mid, "removed": n})

    @api.get("/inbox")
    def inbox(domain: str = "", page: int = 1,
              page_size: int = 50):
        """待整理箱:列表 + 可选的分类树(前端下拉用,按领域分组)。"""
        page, page_size = _page_args(page, page_size)
        rows = memory.db.list_items(status="inbox", domain=domain or None,
                                    limit=page_size, offset=(page - 1) * page_size)
        total = memory.db.count_items(status="inbox", domain=domain or None)
        cats = {}
        for c in memory.db.categories():
            if c.get("status") != "archived":
                cats.setdefault(c["domain"], []).append(ser.category(c))
        return page_payload([ser.item_brief(it) for it in rows], total, page,
                            page_size, categories=cats,
                            domain_counts=memory.db.items_by_domain(status="inbox"))

    @api.post("/inbox/batch")
    def inbox_batch(payload: dict = Body(...)):
        """批量处理:{action, ids?, domain?, category_id?}

        action: filed(归档) | archived(丢弃) | auto(交给 AI 分类) | assign(指定分类)
        ids 缺省时对"该领域(或全部)待整理条目"生效。
        """
        action = (payload.get("action") or "").strip()
        ids = payload.get("ids") or []
        domain = (payload.get("domain") or "").strip() or None
        if action not in ("filed", "archived", "auto", "assign"):
            fail("invalid_action", f"未知批量操作: {action}", 422)
        if action == "assign" and not payload.get("category_id"):
            fail("invalid_action", "assign 需要 category_id", 422)

        targets = [int(i) for i in ids] if ids else [
            it["id"] for it in memory.db.list_items(status="inbox",
                                                    domain=domain, limit=500)]
        if action in ("filed", "archived"):
            for iid in targets:
                memory.db.set_item_status(iid, action)
        elif action == "assign":
            cat = memory.db.get_category(int(payload["category_id"]))
            if not cat:
                fail("not_found", "分类不存在", 404)
            for iid in targets:
                memory.db.set_item_category(iid, cat["id"], None,
                                            f"手动归入:{cat['name']}")
                memory.db.set_item_status(iid, "filed")
        else:  # auto
            for iid in targets:
                memory.db.enqueue("auto_process", {"item_id": iid})
        logger.info("批量处理 %s: %d 条(domain=%s)", action, len(targets), domain)
        return ok({"action": action, "affected": len(targets), "ids": targets})

    @api.get("/search/images")
    def search_images(q: str = "", domain: str = "", top_k: int = 8):
        """文字搜画面(Chinese-CLIP);未装权重时返回空列表而非报错。"""
        if not q.strip():
            return ok({"items": [], "enabled": memory.image_embedder is not None})
        hits = memory.search_images(q, domain=domain or None,
                                    top_k=max(1, min(24, top_k)))
        return ok({"items": [ser.image_hit(h) for h in hits],
                   "enabled": memory.image_embedder is not None, "query": q})

    @api.post("/search/image")
    async def search_by_image(request: Request, file: UploadFile = File(...),
                              top_k: int = 12):
        """以图搜图(JSON 版):上传一张图,返回相似画面列表(含查询图回显地址)。"""
        data = await file.read()
        if not (file.filename or "").strip() or not data:
            fail("empty_file", "没有选择图片或图片为空", 422)
        if len(data) > 8 * 1024 * 1024:
            fail("too_large", "图片太大(限 8MB)", 413)
        ib = memory.image_embedder
        if ib is None:
            fail("image_search_disabled", "图像检索未启用:需要 cn-clip 与权重", 503)
        from memvault.config import media_dir

        upload_dir = media_dir(cfg) / "_queries"
        upload_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
            suffix = ".jpg"
        from datetime import datetime

        dest = upload_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}"
        dest.write_bytes(data)
        try:
            vec = ib.encode_image(str(dest))
            hits = memory.vs.query_image(vec, k=max(1, min(24, top_k)))
        except Exception as e:  # noqa: BLE001 — 单次失败不崩面板
            logger.warning("以图搜图失败:%s", e)
            fail("image_encode_failed", f"以图搜图失败:{e}", 500)
        chunks = {c["id"]: c for c in memory.db.get_chunks(
            [h["chunk_id"] for h in hits])}
        items = []
        for h in hits:
            c = chunks.get(h["chunk_id"])
            if c is None:
                continue
            it = memory.db.get_items([c["item_id"]]).get(c["item_id"])
            if it is None:
                continue
            items.append(ser.image_hit({"score": round(
                1.0 - float(h.get("distance", 1.0)), 4), "chunk": c, "item": it}))
        return ok({"items": items,
                   "query_image": _media_url(cfg, str(dest)),
                   "query_label": file.filename})

    @api.post("/items/{item_id}/similar-image")
    def item_similar_image(request: Request, item_id: int,
                           chunk_id: int = Body(..., embed=True)):
        """用库里已有的某帧找相似画面(条目详情页「🔍 找相似画面」)。"""
        it = memory.get_item(item_id)
        if not it:
            fail("not_found", f"条目不存在: {item_id}", 404)
        chunk = next((c for c in it.get("chunks", [])
                      if c["id"] == chunk_id and c.get("media_path")), None)
        if chunk is None:
            fail("not_found", "该条目下没有这个画面块", 404)
        ib = memory.image_embedder
        if ib is None:
            fail("image_search_disabled", "图像检索未启用:需要 cn-clip 与权重", 503)
        vec = ib.encode_image(chunk["media_path"])
        hits = memory.vs.query_image(vec, k=12)
        chunks = {c["id"]: c for c in memory.db.get_chunks(
            [h["chunk_id"] for h in hits])}
        items = []
        for h in hits:
            c = chunks.get(h["chunk_id"])
            if c is None:
                continue
            row = memory.db.get_items([c["item_id"]]).get(c["item_id"])
            if row is None:
                continue
            items.append(ser.image_hit({"score": round(
                1.0 - float(h.get("distance", 1.0)), 4), "chunk": c, "item": row}))
        return ok({"items": items, "query_image": _media_url(cfg, chunk["media_path"]),
                   "query_label": f"#{item_id} 的画面"})

    @api.get("/categories")
    def categories(domain: str = ""):
        """分类树 + 按领域分组 + UP主规则 + 领域候选(前端下拉用)。"""
        cats = [ser.category(c) for c in memory.db.categories(domain or None)]
        groups: dict = {}
        for c in cats:
            groups.setdefault(c["domain"], []).append(c)
        # 领域候选:已有分类的领域 ∪ 条目用到的领域(前端输入框下拉建议用)
        domains = sorted(set(groups) |
                         {d["domain"] for d in memory.db.items_by_domain()})
        return ok({"items": cats, "groups": groups, "domains": domains,
                   "up_rules": memory.db.up_rules()})

    @api.post("/categories")
    def add_category(domain: str = Body(...), name: str = Body(...)):
        """新增分类(领域可手填,也可用 /api/categories 里的 domains 建议)。"""
        name = (name or "").strip()[:40]
        if not name:
            fail("invalid_name", "分类名不能为空", 422)
        cid = memory.db.add_category((domain or "general").strip(),
                                     name, status="active")
        return ok(ser.category(memory.db.get_category(cid)))

    @api.post("/categories/{category_id}/confirm")
    def confirm_category(category_id: int):
        """采纳 LLM 提议的待确认分类。"""
        if not memory.db.get_category(category_id):
            fail("not_found", f"分类不存在: {category_id}", 404)
        memory.db.confirm_category(category_id)
        return ok(ser.category(memory.db.get_category(category_id)))

    @api.post("/categories/{category_id}/delete")
    def delete_category(category_id: int):
        """删除分类:条目退回待整理箱,专属提示词与 UP 规则一并清理。"""
        info = memory.db.delete_category(category_id)
        if info is None:
            fail("not_found", f"分类不存在: {category_id}", 404)
        logger.info("删除分类 %s/%s:条目退回 %d 条,清理提示词 %d 条、UP规则 %d 条",
                    info["domain"], info["name"], info["items"],
                    info["prompts"], info["up_rules"])
        return ok(info)

    @api.get("/jobs")
    def jobs(limit: int = 50, status: str = ""):
        """任务队列(含 payload 摘要与耗时)。"""
        rows = memory.db.list_jobs(min(200, max(1, limit)))
        data = [ser.job(j) for j in rows
                if not status or j["status"] == status]
        return ok({"items": data, "total": len(data)})

    @api.post("/chat")
    def chat(app_request: Request, message: str = Body(...),
             skill: str = Body("general")):
        """与技能对话(LLM 抽取画像/日志 → 检索个人上下文 → 生成回复)。

        消息本身不落盘,抽取出的画像键值与日志会落盘(见 DESIGN §13.7)。
        """
        text = (message or "").strip()
        if not text:
            fail("empty_message", "消息不能为空", 422)
        llm = getattr(app_request.app.state, "llm", None)
        if llm is None or not getattr(llm, "enabled", False):
            fail("llm_disabled", "LLM 未配置(设置 DEEPSEEK_API_KEY 后重启)", 503)
        from memvault.chat import chat_turn

        try:
            out = chat_turn(memory, llm, text, skill or "general")
        except ValueError as e:
            fail("invalid_message", str(e), 422)
        except Exception as e:  # noqa: BLE001 — 上游 API 异常
            logger.exception("聊天处理失败")
            fail("llm_error", f"LLM 调用失败:{str(e)[:200]}", 502)
        return ok(out)

    # ── 提示词规则绑定(购物稿 ① 的管理面)────────────────────────────
    @api.get("/prompt-bindings")
    def prompt_bindings():
        """规则列表 + 可选的 profile + 已缓存的 UP 画像。"""
        from memvault.prompts import PromptStore

        PromptStore(memory.db).ensure_seed()   # 种子提示词懒建,这里兜一下
        rows = memory.db.prompt_bindings(stage=None, enabled_only=False)
        profiles = [r["name"] for r in memory.db._conn().execute(
            "SELECT DISTINCT name FROM prompts WHERE stage='extract'"
            " AND status='active' ORDER BY name").fetchall()]
        frames_rows = memory.db.prompt_bindings(stage="frames", enabled_only=False)
        caches = []
        for r in frames_rows:
            import json as _json
            try:
                payload = _json.loads(r.get("payload") or "{}")
            except (TypeError, ValueError):
                payload = {}
            caches.append({
                "up_mid": r["target"], "note": r.get("note"),
                "enabled": bool(r.get("enabled")),
                "subtitle_bands": payload.get("subtitle_bands"),
                "event_hz": payload.get("event_hz"),
                "speech_ratio": payload.get("speech_ratio"),
                "duration": payload.get("duration"),
                "updated_at": r.get("created_at"),
            })
        items = []
        for r in rows:
            row = dict(r)
            row["label"] = None
            if row["kind"] == "up":     # 显示 UP 名而不是光秃秃的 mid
                hit = memory.db._conn().execute(
                    "SELECT up_name FROM up_categories WHERE up_mid=?",
                    (row["target"],)).fetchone()
                if hit and hit["up_name"]:
                    row["label"] = hit["up_name"]
                else:
                    import json as _json
                    for it in memory.db._conn().execute(
                            "SELECT attrs_json FROM items WHERE attrs_json LIKE ?"
                            " ORDER BY id DESC LIMIT 5", (f'%{row["target"]}%',)):
                        try:
                            a = _json.loads(it["attrs_json"] or "{}")
                        except (TypeError, ValueError):
                            continue
                        if str(a.get("up_mid")) == str(row["target"]) and a.get("up"):
                            row["label"] = a["up"]
                            break
            items.append(row)
        return ok({"items": items, "profiles": profiles,
                   "kinds": ["up", "up_set", "domain", "source_type", "keyword"],
                   "stages": ["extract", "router", "clip", "frames"],
                   "caches": caches})

    @api.post("/prompt-bindings")
    def prompt_binding_add(payload: dict = Body(...)):
        """绑定一条规则:{kind, target, prompt_name, stage?, note?}。"""
        kind = (payload.get("kind") or "").strip()
        target = str(payload.get("target") or "").strip()
        name = (payload.get("prompt_name") or "").strip()
        stage = (payload.get("stage") or "extract").strip()
        if kind not in ("up", "up_set", "domain", "source_type", "keyword"):
            fail("invalid_kind", f"未知规则类型:{kind}", 422)
        if not target or not name:
            fail("invalid_target", "target 与 prompt_name 都不能为空", 422)
        exists = memory.db._conn().execute(
            "SELECT 1 FROM prompts WHERE name=? AND stage='extract'"
            " AND status='active'", (name,)).fetchone()
        if not exists:
            fail("not_found", f"提示词 profile 不存在:{name}", 404)
        memory.db.bind_prompt(kind, target, name, stage=stage,
                              note=(payload.get("note") or "").strip() or None)
        logger.info("绑定提示词规则:%s=%s → %s(%s)", kind, target, name, stage)
        return ok({"kind": kind, "target": target, "prompt_name": name,
                   "stage": stage})

    @api.post("/prompt-bindings/unbind")
    def prompt_binding_remove(payload: dict = Body(...)):
        n = memory.db.unbind_prompt((payload.get("kind") or "").strip(),
                                    str(payload.get("target") or "").strip(),
                                    stage=(payload.get("stage") or "extract").strip())
        return ok({"removed": n})

    @api.post("/frames-profiles/clear")
    def frames_profile_clear(payload: dict = Body(...)):
        """清掉某 UP 的画像缓存(下次采集会重新生成)。"""
        n = memory.db.unbind_prompt("up", str(payload.get("up_mid") or "").strip(),
                                    stage="frames")
        return ok({"removed": n})

    @api.get("/sources")
    def sources():
        """订阅源列表(B站 UP主等)。"""
        return ok({"items": [ser.source(s) for s in memory.db.watch_sources()]})

    @api.post("/sources")
    def add_source(kind: str = Body("bili_up"), target: str = Body(...),
                   domain: str = Body("general")):
        """新增订阅源(B站 UP主):UID 或 space 链接都能解析。"""
        from memvault.sources import bili_watch

        mid = bili_watch.parse_mid(target)
        if not mid:
            fail("invalid_target", "无法解析 UP主 UID(需 UID 或 space 链接)", 422)
        label = bili_watch.up_name(mid)
        sid = memory.db.add_watch_source(kind, mid,
                                         domain=(domain or "general"), label=label)
        logger.info("新增订阅源 %s(%s) → 领域 %s", label, mid, domain)
        return ok({"id": sid, "kind": kind, "target": mid, "label": label,
                   "domain": domain or "general"})

    @api.post("/sources/{source_id}/toggle")
    def toggle_source(source_id: int):
        """启用/停用订阅源。"""
        src = memory.db.get_watch_source(source_id)
        if not src:
            fail("not_found", f"订阅源不存在: {source_id}", 404)
        memory.db.toggle_watch_source(source_id)
        return ok(ser.source(memory.db.get_watch_source(source_id)))

    @api.post("/sources/{source_id}/check")
    def check_source(source_id: int):
        """立即检查该订阅源(入队 watch_check)。"""
        src = memory.db.get_watch_source(source_id)
        if not src:
            fail("not_found", f"订阅源不存在: {source_id}", 404)
        memory.db.enqueue("watch_check", {"source_id": source_id})
        return ok({"source_id": source_id, "queued": "watch_check"})

    @api.get("/stats")
    def stats():
        """库概览:条目/语义块/待整理/日志等计数 + 领域分布。"""
        base = memory.db.stats()
        return ok({**base, "domains": memory.db.items_by_domain(),
                   "pending_images": memory.db._conn().execute(
                       "SELECT COUNT(*) FROM chunks WHERE modality='image'"
                       " AND embed_status='pending'").fetchone()[0]})

    @api.get("/settings")
    def settings(app_request: Request):
        """当前配置(**密钥只回显是否已配置,绝不返回明文**)。

        api_key_set 以"运行中客户端实际持有的 key"为准(与页面路由一致),
        没有客户端时退回配置解析结果。
        """
        from memvault.llm import resolve_api_key

        live = getattr(app_request.app.state, "llm", None)
        key = (getattr(live, "api_key", "") or resolve_api_key(cfg) or "")
        llm = dict(cfg.get("llm", {}))
        llm.pop("api_key", None)
        llm.pop("web", None)
        llm["api_key_set"] = bool(key)
        return ok({"llm": llm, "asr": cfg.get("asr", {}),
                   "embedding": cfg.get("embedding", {}),
                   "vision": cfg.get("vision", {}),
                   "frames": cfg.get("frames", {}),
                   "links": cfg.get("links", {}),
                   "clips": cfg.get("clips", {}),
                   "watch": cfg.get("watch", {})})

    @api.post("/settings")
    def save_settings(app_request: Request, backend: str = Body("api"),
                      base_url: str = Body(...), model: str = Body(...),
                      api_key: str = Body(""),
                      classify_confidence: float = Body(0.8),
                      ads_policy: str = Body("ignore")):
        """保存 LLM 设置(写 config.yaml + .env 并即时生效)。

        **密钥只写不回读**:api_key 传空字符串表示"不改动现有 key"。
        """
        import yaml as _yaml

        from memvault import llm as llm_mod
        from memvault.config import DEFAULT_CONFIG_PATH

        p = Path(cfg.get("_config_path") or DEFAULT_CONFIG_PATH)
        raw = {}
        if p.exists():
            raw = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        sec = raw.setdefault("llm", {})
        sec["backend"] = backend if backend in ("api", "web") else "api"
        sec["base_url"] = (base_url or "").strip()
        sec["model"] = (model or "").strip()
        sec["classify_confidence"] = float(classify_confidence)
        sec["ads_policy"] = ads_policy if ads_policy in ("ignore", "mention") else "ignore"
        p.write_text(_yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                     encoding="utf-8")

        cfg["llm"]["backend"] = sec["backend"]
        cfg["llm"]["base_url"] = sec["base_url"]
        cfg["llm"]["model"] = sec["model"]
        cfg["llm"]["classify_confidence"] = sec["classify_confidence"]
        cfg["llm"]["ads_policy"] = sec["ads_policy"]
        key_updated = bool((api_key or "").strip())
        if key_updated:
            env = llm_mod.PROJECT_ROOT / ".env"
            lines = (env.read_text(encoding="utf-8").splitlines()
                     if env.exists() else [])
            lines = [x for x in lines if not x.startswith("DEEPSEEK_API_KEY=")]
            lines.append(f"DEEPSEEK_API_KEY={api_key.strip()}")
            env.write_text("\n".join(lines) + "\n", encoding="utf-8")
            cfg["llm"]["api_key"] = api_key.strip()

        llm_mod.reset_llm_client()        # 切换后端时关闭旧的浏览器实例
        app_request.app.state.llm = llm_mod.get_llm_client(cfg)
        logger.info("LLM 设置已保存: backend=%s model=%s",
                    sec["backend"], sec["model"])
        from memvault.llm import resolve_api_key

        out = dict(sec)
        out.pop("web", None)
        out["api_key_set"] = bool(resolve_api_key(cfg))
        out["api_key_updated"] = key_updated
        return ok(out)

    @api.post("/settings/test")
    def test_settings(app_request: Request):
        """测试 LLM 连通性:返回 {ok, message}。"""
        client = getattr(app_request.app.state, "llm", None)
        if client is None:
            from memvault.llm import get_llm_client

            client = get_llm_client(cfg)
        if not getattr(client, "enabled", False):
            fail("llm_disabled", "未配置 API key", 400)
        try:
            # 预算给足:推理型模型的思考 token 会占用 max_tokens
            client.chat_json("你是回声机,只输出 JSON。", '回复 {"ok": true}',
                             max_tokens=800)
            return ok({"message": f"连接成功 ✓ 当前模型:{client.model}"})
        except Exception as e:  # noqa: BLE001
            fail("llm_error", f"连接失败 ✗ {str(e)[:160]}", 502)

    return api
