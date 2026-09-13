"""LLM 自动分类:路由(选桶)+ 提取(按分类提示词)。DESIGN §13.1 流程 A。"""
import json
import logging

logger = logging.getLogger(__name__)


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
    if new_cat and conf >= threshold:
        cid = memory.db.add_category(item["domain"], str(new_cat)[:40],
                                     status="proposed")
        memory.db.set_item_category(item_id, cid, conf,
                                    f"提议新分类:{new_cat}。{reason}")
        # 提议中的分类不转 filed,等用户确认;条目留在待整理箱
        logger.info("item=%s 提议新分类 '%s'(待确认)", item_id, new_cat)
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

    merged = {}
    try:
        merged.update(json.loads(item.get("attrs_json") or "{}"))
    except json.JSONDecodeError:
        pass
    for k, v in resp.items():
        if v is not None:
            merged[k] = v
    memory.db._conn().execute(
        "UPDATE items SET attrs_json=? WHERE id=?",
        (json.dumps(merged, ensure_ascii=False), item_id),
    )
    memory.db._conn().commit()
    return {"attrs": merged, "category": cat_name}
