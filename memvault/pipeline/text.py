"""文本/商品采集处理器(网页选段、页面正文、商品页)。"""
import logging

logger = logging.getLogger(__name__)


def ingest_text(payload: dict, memory) -> int:
    """网页选段 / 页面正文入库。

    payload: {title, url, text, domain?}
    """
    text = (payload.get("text") or "").strip()
    title = (payload.get("title") or text[:40] or "未命名摘录").strip()
    url = payload.get("url")
    if not text:
        raise ValueError("采集内容为空")

    item_id = memory.add_item(
        domain=payload.get("domain", "general"),
        type_="note" if payload.get("kind") == "selection" else "page",
        title=title,
        content_text=text[:4000],
        source_type="webpage",
        source_ref=url,
    )
    memory.add_text_chunk(item_id, text[:4000])
    logger.info("文本入库 item=%s (%s)", item_id, title[:30])
    return item_id


def ingest_product(payload: dict, memory) -> int:
    """商品页入库(个人库/愿望清单语义)。

    payload: {url, title, product:{name, price, image_url, shop}, domain?}
    """
    p = payload.get("product") or {}
    name = (p.get("name") or payload.get("title") or "未知商品").strip()
    url = payload.get("url")
    attrs = {
        k: p[k] for k in ("price", "image_url", "shop") if p.get(k)
    }
    item_id = memory.add_item(
        domain=payload.get("domain", "shopping"),
        type_="product",
        title=name,
        attrs=attrs,
        source_type="webpage",
        source_ref=url,
    )
    summary = " ".join(
        f"{k}:{v}" for k, v in attrs.items()
    )
    memory.add_text_chunk(item_id, f"{name} {summary} {url or ''}")
    logger.info("商品入库 item=%s (%s)", item_id, name[:30])
    return item_id
