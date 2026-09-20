"""B站弹幕 → 广告段标注(设计稿 design-frame-units.md §2.7 / 第 5 步)。

用途:观众比 UP 更早承认"这段是广告"——弹幕里的「接广/金主/甲方/催更/赞助/恰饭」
集中在广告段。用它给条目标出广告时间段(只标时间,不做语义处理)。

两个实测细节:
- 弹幕接口返回的 `Content-Encoding: deflate` 是**裸 deflate**,要 `zlib.decompress(body, -15)`
  (直接 `zlib.decompress` 会报 "invalid header");
- 单条弹幕不算数:**同一时间窗内 ≥2 条**命中才判广告段(实测可滤掉观众拿代言梗开玩笑的误报)。
"""
import logging
import re
import zlib

logger = logging.getLogger(__name__)

DANMAKU_URL = "https://comment.bilibili.com/{cid}.xml"

# 词表要含观众黑话(设计稿 §1.7)
AD_WORDS = ("接广", "恰饭", "广子", "金主", "甲方", "赞助", "催更", "广告",
            "商单", "带货", "硬广", "软广", "接了", "口播")
_AD_RE = re.compile(r"<d p=\"([^\"]+)\">(.*?)</d>", re.S)


def fetch_danmaku(cid, cookies_path: str | None = None, timeout: int = 20):
    """抓弹幕,返回 [{"ts": 秒, "text": 文本, "mode": 模式}]。

    失败返回空列表(弹幕拿不到不影响采集)。
    """
    import requests

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if cookies_path:
        try:
            from memvault.sources.bili_downloader import BiliCookie

            headers["Cookie"] = BiliCookie.to_header(cookies_path)
        except Exception as e:  # noqa: BLE001 — 没 cookies 也能抓公开弹幕
            logger.info("弹幕:读取 cookies 失败(%s),按匿名抓取", e)
    try:
        r = requests.get(DANMAKU_URL.format(cid=cid), headers=headers,
                         timeout=timeout)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        logger.info("弹幕抓取失败 cid=%s: %s", cid, e)
        return []
    raw = r.content
    text = None
    for attempt in (lambda b: zlib.decompress(b, -15),      # 裸 deflate(实测)
                    lambda b: zlib.decompress(b),
                    lambda b: b):
        try:
            text = attempt(raw).decode("utf-8", errors="ignore")
            break
        except Exception:  # noqa: BLE001 — 换下一种解压方式
            continue
    if not text:
        logger.info("弹幕解压失败 cid=%s(长度 %d)", cid, len(raw))
        return []
    out = []
    for p, content in _AD_RE.findall(text):
        parts = p.split(",")
        try:
            ts = float(parts[0])
        except (ValueError, IndexError):
            continue
        out.append({"ts": ts, "text": content.strip(),
                    "mode": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1})
    logger.info("弹幕 %d 条(cid=%s)", len(out), cid)
    return out


def detect_ad_segments(danmaku: list[dict], window: float = 10.0,
                       min_hits: int = 2) -> list[tuple[float, float]]:
    """按"时间窗内命中 ≥min_hits 条广告词"判广告段,返回 [(start, end)]。

    窗口按弹幕时间对齐(window 秒一格);相邻命中窗合并成段。
    """
    if not danmaku:
        return []
    hits = []
    for d in danmaku:
        text = d.get("text") or ""
        if any(w in text for w in AD_WORDS):
            hits.append(float(d.get("ts") or 0.0))
    if not hits:
        return []
    buckets: dict[int, int] = {}
    for ts in hits:
        buckets[int(ts // window)] = buckets.get(int(ts // window), 0) + 1
    hot = sorted(k for k, n in buckets.items() if n >= min_hits)
    if not hot:
        return []
    segments, start, prev = [], hot[0], hot[0]
    for k in hot[1:]:
        if k - prev <= 1:        # 相邻窗 → 同一段
            prev = k
            continue
        segments.append((start * window, (prev + 1) * window))
        start = prev = k
    segments.append((start * window, (prev + 1) * window))
    return segments


def mark_ad_segments(memory, item_id: int, cid, cookies_path=None,
                     window: float = 10.0, min_hits: int = 2) -> list:
    """抓弹幕 → 判广告段 → 写进条目 attrs_json 的 `ad_segments`(只标时间段)。"""
    import json

    danmaku = fetch_danmaku(cid, cookies_path=cookies_path)
    if not danmaku:
        return []
    segs = detect_ad_segments(danmaku, window=window, min_hits=min_hits)
    item = memory.get_item(item_id)
    if not item:
        return []
    attrs = json.loads(item.get("attrs_json") or "{}")
    attrs["ad_segments"] = [{"start": round(a, 1), "end": round(b, 1)}
                            for a, b in segs]
    attrs["danmaku_count"] = len(danmaku)
    memory.db._conn().execute(
        "UPDATE items SET attrs_json=? WHERE id=?",
        (json.dumps(attrs, ensure_ascii=False), item_id))
    memory.db._conn().commit()
    if segs:
        logger.info("item=%s 弹幕判出广告段 %s", item_id,
                    ", ".join(f"{a:.0f}~{b:.0f}s" for a, b in segs))
    return segs
