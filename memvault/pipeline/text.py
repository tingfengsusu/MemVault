"""文本/商品采集处理器(网页选段、页面正文、商品页)。"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_IMG_TIMEOUT = 20
_IMG_MAX_BYTES = 12 * 1024 * 1024   # 主图上限,避免把巨图塞进库里
_IMG_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
            "image/gif": ".gif", "image/bmp": ".bmp"}


def download_image(url: str, dest_dir, stem: str = "product"):
    """下载商品主图到本地,返回路径;失败返回 None(不影响条目入库)。"""
    if not url or not str(url).lower().startswith(("http://", "https://")):
        return None
    import requests

    try:
        r = requests.get(url, timeout=_IMG_TIMEOUT, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": str(url),
        })
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001 — 主图拿不到不影响条目
        logger.info("商品主图下载失败(%s): %s", str(url)[:60], e)
        return None
    body = r.content or b""
    if not body or len(body) > _IMG_MAX_BYTES:
        logger.info("商品主图跳过(大小 %d 字节)", len(body))
        return None
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
    ext = _IMG_EXT.get(ctype)
    if ext is None:
        guess = str(url).split("?")[0].rsplit(".", 1)[-1].lower()
        ext = f".{guess}" if guess in ("jpg", "jpeg", "png", "webp", "gif",
                                       "bmp") else ".jpg"
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{stem}{ext}"
    path.write_bytes(body)
    return str(path)


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


def ingest_product(payload: dict, memory, cfg: dict = None) -> int:
    """商品页入库(个人库/愿望清单语义)。

    payload: {url, title, product:{name, price, image_url, shop}, domain?}

    主图会下载到 media/item_<id>/ 并建成**图像向量块**:"以图找同款"
    (传一张图,找库里已有的相似商品)靠的就是它。
    """
    from memvault.config import media_dir

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

    # 主图:本地存档 + 图像向量(下载失败不影响条目本身)
    media_paths = []
    if p.get("image_url") and cfg is not None:
        local = download_image(p["image_url"],
                               media_dir(cfg) / f"item_{item_id}")
        if local:
            media_paths = [local]
            memory.add_image_chunk(item_id, local, seq=0,
                                   image_embedder=memory.image_embedder)
            memory.db.update_item_media(item_id, media_paths=media_paths)

    logger.info("商品入库 item=%s (%s)%s", item_id, name[:30],
                "含主图" if media_paths else "")
    return item_id
