"""LLM 自动分类:路由(选桶)+ 提取(按分类提示词)。DESIGN §13.1 流程 A。"""
import json
import logging
import re

logger = logging.getLogger(__name__)

# LLM 常把分类提议写进 reason 而漏掉结构化字段,这里做兜底解析
_PROPOSAL_RE = re.compile(
    r"(?:归入|归类为|建议(?:新增|新建|添加)|新建)\s*[「\"']?"
    r"([^\s(（\[,。;:「」\"']{2,15})"
)


def _proposal_from_reason(reason: str) -> str | None:
    m = _PROPOSAL_RE.search(reason or "")
    if not m:
        return None
    name = m.group(1).rstrip("分类标签类目")
    return name or None


def _tree_text(categories: list[dict]) -> str:
    if not categories:
        return "(空,该领域还没有任何分类)"
    return "\n".join(
        f"- id={c['id']} {c['name']}"
        + ("(待确认)" if c.get("status") == "proposed" else "")
        for c in categories if c.get("status") != "archived"
    )


def _item_text(item: dict) -> str:
    parts = [f"标题:{item['title']}", f"类型:{item['type']}"]
    if item.get("content_text"):
        parts.append(f"内容:{item['content_text'][:1500]}")
    return "\n".join(parts)


def route_item(memory, llm, pstore, item_id: int, cfg: dict) -> dict:
    """路由:返回 {action: filed|proposed|inbox, category_id, confidence, reason}。"""
    item = memory.get_item(item_id)
    if not item:
        raise ValueError(f"条目不存在: {item_id}")

    cats = memory.db.categories(item["domain"])
    valid_ids = {c["id"] for c in cats if c.get("status") == "active"}
    similar = memory.search(
        f"{item['title']} {item.get('content_text') or ''}"[:300],
        domain=item["domain"], top_k=3,
    )
    similar_text = "\n".join(
        f"- 标题:{s['item']['title']} 分类id={s['item']['category_id']}"
        for s in similar if s["item"]["id"] != item_id
    ) or "(无相似条目)"

    prompt = pstore.get_active("router", "classify")
    if prompt is None:
        pstore.ensure_seed()
        prompt = pstore.get_active("router", "classify")
    user = (
        f"分类树:\n{_tree_text(cats)}\n\n"
        f"相似条目参考:\n{similar_text}\n\n"
        f"待分类条目:\n{_item_text(item)}"
    )
    resp = llm.chat_json(prompt["content"], user)
    conf = float(resp.get("confidence") or 0)
    threshold = cfg.get("llm", {}).get("classify_confidence", 0.8)
    reason = str(resp.get("reason", ""))[:200]

    cid = resp.get("category_id")
    if cid in valid_ids and conf >= threshold:
        memory.db.set_item_category(item_id, cid, conf, reason)
        memory.db.set_item_status(item_id, "filed")
        logger.info("item=%s 路由到分类 %s (conf=%.2f)", item_id, cid, conf)
        return {"action": "filed", "category_id": cid,
                "confidence": conf, "reason": reason}

    new_cat = (resp.get("new_category") or {}).get("name")
    if not new_cat:  # 兜底:从 reason 文本里抽取"归入XX"类提议
        new_cat = _proposal_from_reason(reason)
        if new_cat:
            logger.info("item=%s 从 reason 兜底解析出分类提议: %s",
                        item_id, new_cat)
    # 提议新分类是"待用户确认"的动作,阈值可比直接归档低,命中率优先
    propose_threshold = min(threshold, 0.5)
    if new_cat and conf >= propose_threshold:
        name = str(new_cat).strip().strip("「」\"'")[:40]
        # 提议查重:同名分类已存在则复用,不再重复创建
        existing = next((c for c in cats
                         if c["name"] == name and c.get("status") != "archived"),
                        None)
        if existing is not None:
            cid = existing["id"]
            if existing.get("status") == "active" and conf >= threshold:
                memory.db.set_item_category(item_id, cid, conf, reason)
                memory.db.set_item_status(item_id, "filed")
                logger.info("item=%s 命中已有分类「%s」(active) → filed",
                            item_id, name)
                return {"action": "filed", "category_id": cid,
                        "confidence": conf, "reason": reason}
            memory.db.set_item_category(
                item_id, cid, conf, f"命中已有分类「{name}」。{reason}")
            logger.info("item=%s 复用已有分类「%s」", item_id, name)
            return {"action": "proposed", "category_id": cid,
                    "confidence": conf, "reason": reason}

        cid = memory.db.add_category(item["domain"], name, status="proposed")
        memory.db.set_item_category(item_id, cid, conf, f"提议新分类:{name}。{reason}")
        logger.info("item=%s 提议新分类「%s」(待确认)", item_id, name)
        return {"action": "proposed", "category_id": cid,
                "confidence": conf, "reason": reason}

    memory.db.set_item_category(item_id, None, conf, f"未分类:{reason}")
    return {"action": "inbox", "category_id": None,
            "confidence": conf, "reason": reason}


def extract_item(memory, llm, pstore, item_id: int) -> dict:
    """按分类提示词提取结构化属性,合并进 attrs_json。"""
    item = memory.get_item(item_id)
    if not item:
        raise ValueError(f"条目不存在: {item_id}")
    category = memory.db.get_category(item["category_id"]) \
        if item.get("category_id") else None
    cat_name = category["name"] if category else "通用"

    prompt = None
    if category:
        prompt = pstore.get_active("extract", "extract", category["id"])
    if prompt is None:
        prompt = pstore.get_active("extract", "extract", None)
        if prompt is None:
            pstore.ensure_seed()
            prompt = pstore.get_active("extract", "extract", None)
    system = prompt["content"].replace("{category}", cat_name)

    text = item.get("content_text") or ""
    if not text:
        text = "\n".join(
            c["content"] for c in item.get("chunks", [])
            if c["modality"] == "text" and c.get("content")
        )[:2000]
    resp = llm.chat_json(system, f"条目标题:{item['title']}\n条目内容:\n{text[:3000]}")

    # 写入独立的 attrs_ai 字段:整体替换保证"重新分析"幂等
    # (attrs_json 保留给采集时预写的原始属性,如商品价格/店铺)
    extracted = {k: v for k, v in resp.items() if v is not None}
    memory.db._conn().execute(
        "UPDATE items SET attrs_ai=? WHERE id=?",
        (json.dumps(extracted, ensure_ascii=False), item_id),
    )
    memory.db._conn().commit()
    return {"attrs": extracted, "category": cat_name}
