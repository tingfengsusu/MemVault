"""双链构建:基于向量相似度为条目计算"相关条目",形成显式知识网络。

思路(卢曼卡片盒的链接层):
  条目文本嵌入 → 向量库近邻(带 item_id)→ 按条目聚合取最大相似度
  → 超过阈值且排名靠前的存为双向链接(整体替换,幂等)。
链接的用途:库页/条目页"相关条目"、检索的第二跳邻居扩展。
"""
import logging

logger = logging.getLogger(__name__)


def build_links_for_item(memory, item_id: int, cfg: dict) -> list[tuple[int, float]]:
    """计算并保存某条目的相关条目,返回 [(related_id, score)]。"""
    item = memory.get_item(item_id)
    if not item:
        return []
    lcfg = cfg.get("links", {})
    threshold = float(lcfg.get("similarity_threshold", 0.55))
    top_n = int(lcfg.get("max_per_item", 3))

    # 查询文本优先用正文(与入库块文本一致,相似度信号更纯),无正文退化为标题
    text = (item.get("content_text") or "").strip()[:600] or item["title"]
    if not text:
        return []
    vec = memory.embedder.encode([text])[0]
    try:
        hits = memory.vs.query_text(vec, k=12)
    except Exception as e:  # noqa: BLE001 — 向量库异常不影响主流程
        logger.warning("item=%s 链接计算失败:%s", item_id, e)
        return []

    best: dict[int, float] = {}
    for h in hits:
        meta = h.get("metadata") or {}
        rid = meta.get("item_id")
        if not rid or rid == item_id:
            continue
        sim = 1.0 - float(h.get("distance", 1.0))  # cosine 距离 → 相似度
        if sim > best.get(rid, 0.0):
            best[rid] = sim

    related = [(rid, sim) for rid, sim in best.items() if sim >= threshold]
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
