"""LLM 自动分类:路由(选桶)+ 提取(按分类提示词)。DESIGN §13.1 流程 A。"""
import json
import logging
import re

logger = logging.getLogger(__name__)

# LLM 常把分类提议写进 reason 而漏掉结构化字段,这里做兜底解析。
# 分两档措辞:显式档(明确要求"归入/新建"某分类)可信度高;弱档只是
# 顺口一句"属于X类",名字可能是描述词,需要额外证据才会采纳。
_PROPOSAL_STRONG_RE = re.compile(
    r"(?:归入|归类为|归为|划入|建议(?:新增|新建|添加|建立)|新建|增设)"
    r"\s*[「\"']?([^\s、，(（\[,。;:「」\"']{2,15})"
)
_PROPOSAL_WEAK_RE = re.compile(
    r"(?:属于|应归|可归)\s*[「\"']?([^\s、，(（\[,。;:「」\"']{2,15})"
)

# 从理由文本抓来的名字常带语气填充词,剥离后再用(否则会造出
# "心理学书籍最合适"这种和已有分类仅差后缀的重复)
_FILLER_TAIL = ("最合适", "较合适", "更合适", "比较合适", "最为合适",
                "合适", "为宜", "较好", "最佳", "妥当", "最好", "即可")

# 名字尾部常挂的泛化词:"影视解读范畴"与"影视解读"是同一个桶(真实 #13)
_GENERIC_TAIL = ("分类", "类目", "范畴", "标签", "类型", "类别", "主题",
                 "话题", "领域", "条目", "内容")


def same_category_name(existing: str, candidate: str) -> bool:
    """两个分类名是否指同一个桶:完全同名,或仅差一个泛化词尾。"""
    if not existing or not candidate:
        return False
    if existing == candidate:
        return True
    return any(candidate == existing + tail or existing == candidate + tail
               for tail in _GENERIC_TAIL)


def _clean_proposal_name(raw: str) -> str | None:
    name = (raw or "").rstrip("分类标签类目")
    # 长词优先匹配("比较合适" 必须先于 "较合适")
    for filler in sorted(_FILLER_TAIL, key=len, reverse=True):
        if name.endswith(filler):
            name = name[:-len(filler)]
            break
    # 泛化词尾按词剥离(比逐字 rstrip 安全),剥完至少留两个字
    for tail in _GENERIC_TAIL:
        if name.endswith(tail) and len(name) - len(tail) >= 2:
            name = name[:-len(tail)]
            break
    return name or None


def _proposal_hint(reason: str) -> tuple[str | None, bool]:
    """解析理由文本里的分类提议,返回 (名字, 是否属于显式新建要求)。"""
    text = reason or ""
    m = _PROPOSAL_STRONG_RE.search(text)
    explicit = m is not None
    if m is None:
        m = _PROPOSAL_WEAK_RE.search(text)
    if m is None:
        return None, False
    return _clean_proposal_name(m.group(1)), explicit


def _proposal_from_reason(reason: str) -> str | None:
    return _proposal_hint(reason)[0]


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


def up_category_rule(memory, item: dict) -> tuple[int, str] | None:
    """条目所属 UP 是否绑定了分类,返回 (category_id, up_name)。

    绑定信息存在条目的原始属性 attrs_json 里(采集时写入 up/up_mid)。
    """
    import json

    try:
        attrs = json.loads(item.get("attrs_json") or "{}")
    except (TypeError, ValueError):
        attrs = {}
    up_mid = attrs.get("up_mid")
    if not up_mid:
        return None
    rule = memory.db.up_category(up_mid)
    if not rule:
        return None
    return int(rule["category_id"]), (rule.get("up_name") or attrs.get("up")
                                      or str(up_mid))


def route_item(memory, llm, pstore, item_id: int, cfg: dict) -> dict:
    """路由:返回 {action: filed|proposed|inbox, category_id, confidence, reason}。"""
    item = memory.get_item(item_id)
    if not item:
        raise ValueError(f"条目不存在: {item_id}")

    # UP主规则优先:用户给某个 UP 绑了分类,他的视频直接归档,不问 LLM
    up_rule = up_category_rule(memory, item)
    if up_rule:
        cid, name = up_rule
        note = f"UP主规则:{name} 的视频归入本分类"
        memory.db.set_item_category(item_id, cid, 1.0, note)
        memory.db.set_item_status(item_id, "filed")
        logger.info("item=%s 命中 UP主规则「%s」→ 分类 %s", item_id, name, cid)
        return {"action": "filed", "category_id": cid, "confidence": 1.0,
                "reason": note, "by": "up_rule"}

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
    explicit = bool(new_cat)  # 结构化字段 = LLM 主动点名的新分类
    if not new_cat:  # 兜底:从 reason 文本里抽取"归入XX"类提议
        new_cat, explicit = _proposal_hint(reason)
        if new_cat:
            logger.info("item=%s 从 reason 兜底解析出分类提议: %s(%s)",
                        item_id, new_cat, "显式" if explicit else "弱措辞")
    # 提议是"待用户确认"的动作,阈值可比直接归档低,命中率优先;
    # 显式点名不再看置信度(用户一键采纳/驳回),弱措辞才要求中等置信度
    propose_threshold = min(threshold, 0.5)
    if new_cat and (explicit or conf >= propose_threshold):
        name = str(new_cat).strip().strip("「」\"'")[:40]
        # 提议查重:同名(含"影视解读/影视解读范畴"这类词尾变体)则复用
        existing = next((c for c in cats
                         if c.get("status") != "archived"
                         and same_category_name(c["name"], name)),
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


def _extraction_text(item: dict, budget: int = 3000) -> str:
    """组装提取提示词用的正文。

    长文不能只喂开头:44 分钟视频正文 1.6 万字,截前 3000 字等于只覆盖 1/5,
    属性与结论会漏掉后段(真实 #11 覆盖 18%、#13 覆盖 26%)。
    改为「开头整段 + 中段均匀抽样 + 结尾整段」,总量仍控制在预算内。
    """
    text = (item.get("content_text") or "").strip()
    if not text:
        text = "\n".join(
            c["content"] for c in item.get("chunks", [])
            if c["modality"] == "text" and c.get("content")
        ).strip()
    if len(text) <= budget:
        return text
    sep = "\n……(省略)……\n"
    head_chars = budget * 2 // 5
    n_slices = 6
    # 把省略标记的开销也算进预算,保证总长度可控
    slice_chars = max(40, (budget - head_chars - len(sep) * n_slices) // n_slices)
    rest = text[head_chars:]
    span = max(1, len(rest) // n_slices)
    parts = [text[:head_chars]]
    for i in range(n_slices):
        if i == n_slices - 1:
            seg = rest[-slice_chars:]          # 结尾常是总结/结论,整段保留
        else:
            seg = rest[i * span:(i + 1) * span][:slice_chars]
        if seg.strip():
            parts.append(seg)
    return sep.join(parts)


# 广告/推广内容的处理策略(用户可在设置页切换 llm.ads_policy)
ADS_POLICIES = {
    "ignore": (
        "广告与推广内容一律忽略:不要提取、不要写进任何属性,也不要出现在其他属性的"
        "描述里;只有当整条内容几乎只有广告时,才用一条属性写明「内容性质:推广内容」。"),
    "mention": (
        "广告与推广内容单独归入一条属性(如「推广信息」),与正文信息分开列出,"
        "不要混进其他属性。"),
}


def ads_policy_text(cfg: dict) -> str:
    mode = str((cfg.get("llm") or {}).get("ads_policy", "ignore")).lower()
    return ADS_POLICIES.get(mode, ADS_POLICIES["ignore"])


def extract_item(memory, llm, pstore, item_id: int, cfg: dict | None = None) -> dict:
    """按分类提示词提取结构化属性,写入 items.attrs_ai(整体替换)。"""
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
    system = (prompt["content"] or "").replace("{category}", cat_name)
    # 广告策略:占位符存在就替换;老版本提示词(无占位符)则追加一段,
    # 保证用户改写过的提示词也能用上这个开关
    policy = ads_policy_text(cfg or {})
    if "{ads_policy}" in system:
        system = system.replace("{ads_policy}", policy)
    else:
        system = f"{system}\n- {policy}"

    resp = llm.chat_json(
        system, f"条目标题:{item['title']}\n条目内容:\n{_extraction_text(item)}")

    # 写入独立的 attrs_ai 字段:整体替换保证"重新分析"幂等
    # (attrs_json 保留给采集时预写的原始属性,如商品价格/店铺)
    extracted = {k: v for k, v in resp.items() if v is not None}
    memory.db._conn().execute(
        "UPDATE items SET attrs_ai=? WHERE id=?",
        (json.dumps(extracted, ensure_ascii=False), item_id),
    )
    memory.db._conn().commit()
    return {"attrs": extracted, "category": cat_name}
