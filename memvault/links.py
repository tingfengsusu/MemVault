"""双链构建:基于向量相似度为条目计算"相关条目",形成显式知识网络。

思路(卢曼卡片盒的链接层):
  条目文本嵌入 → 向量库近邻(带 item_id)→ 按条目聚合取最大相似度
  → 超过阈值且排名靠前的存为双向链接(整体替换,幂等)。
链接的用途:库页/条目页"相关条目"、检索的第二跳邻居扩展。
"""
import logging

logger = logging.getLogger(__name__)


def item_profile_text(item: dict, limit: int = 600) -> str:
    """条目用于比较的"画像文本"。

    优先 AI 提取的摘要(attrs_ai 的值):它干净、是内容概述;原始 ASR 正文
    充满"朋友们/对吧/所以说"这类口语填充词,任何两块都容易撞词,是噪音来源。
    没有 AI 摘要时退回标题 + 正文开头。
    """
    import json

    ai = {}
    try:
        ai = json.loads(item.get("attrs_ai") or "{}")
    except (TypeError, ValueError):
        ai = {}
    if ai:
        parts = [str(v) for v in ai.values() if v]
        text = "\n".join(parts)
        if text.strip():
            return f"{item['title']}\n{text}"[:limit]
    return ((item.get("content_text") or "").strip()[:limit]
            or item["title"])[:limit]


def _top_mean(sims: list[float], k: int = 3) -> float:
    """取最高的 k 个相似度的均值。

    以前取"与任意单个块的最大值",噪音太大:只要有一段凑巧撞词就入选;
    均值能把"偶发撞词"摊平(用户实测:#11×#13 最大块 0.64,条目级只有 0.51)。
    """
    if not sims:
        return 0.0
    top = sorted(sims, reverse=True)[:k]
    return sum(top) / len(top)


def build_links_for_item(memory, item_id: int, cfg: dict) -> list[tuple[int, float]]:
    """计算并保存某条目的相关条目,返回 [(related_id, score)]。"""
    item = memory.get_item(item_id)
    if not item:
        return []
    lcfg = cfg.get("links", {})
    threshold = float(lcfg.get("similarity_threshold", 0.55))
    top_n = int(lcfg.get("max_per_item", 3))
    pool = int(lcfg.get("candidate_chunks", 40))

    text = item_profile_text(item)
    if not text.strip():
        return []
    vec = memory.embedder.encode([text])[0]
    try:
        # 排除自己的块:长视频自己的块会占满 top-k,导致"找不到任何相关条目"
        # (真实 #11 有 67 块,查 40 个候选里全是它自己)
        hits = memory.vs.query_text(
            vec, where={"item_id": {"$ne": item_id}}, k=pool)
    except Exception as e:  # noqa: BLE001 — 向量库异常不影响主流程
        logger.warning("item=%s 链接计算失败:%s", item_id, e)
        return []

    per_item: dict[int, list[float]] = {}
    for h in hits:
        meta = h.get("metadata") or {}
        rid = meta.get("item_id")
        if not rid or rid == item_id:
            continue
        per_item.setdefault(rid, []).append(
            1.0 - float(h.get("distance", 1.0)))  # cosine 距离 → 相似度

    scored = [(rid, _top_mean(sims)) for rid, sims in per_item.items()]
    related = [(rid, score) for rid, score in scored if score >= threshold]
    related.sort(key=lambda x: -x[1])
    related = related[:top_n]

    memory.db.set_links(item_id, related)
    if related:
        logger.info("item=%s 相关条目:%s", item_id,
                    ", ".join(f"#{r}({s:.2f})" for r, s in related))
    return related


def rebuild_all(memory, cfg: dict, progress=print) -> dict:
    """为全库条目重建链接(backfill / 换嵌入模型后)。"""
    rows = memory.db._conn().execute(
        "SELECT id FROM items WHERE status != 'archived' ORDER BY id").fetchall()
    total_links = 0
    for i, r in enumerate(rows):
        total_links += len(build_links_for_item(memory, r["id"], cfg))
        if progress and (i + 1) % 20 == 0:
            progress(f"  已处理 {i + 1}/{len(rows)} 条")
    return {"items": len(rows), "links": total_links}
