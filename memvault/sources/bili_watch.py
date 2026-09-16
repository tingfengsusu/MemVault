"""B站 UP主投稿订阅:登录 Cookie + wbi 签名 + 最新视频列表。

target 接受 UP主 UID 或 space.bilibili.com 链接。
B站对匿名访问投稿列表有风控(-352),需在 config.yaml 配置
bili.cookies_path 指向导出的 cookies.txt(登录 B站后导出)。
首次检查只登记历史(种入 done 任务防重),此后增量入库。
"""
import hashlib
import logging
import re
import time
from urllib.parse import quote

import requests

from memvault.sources.bili_downloader import BiliCookie

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# B站 wbi 混淆表(公开算法)
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62,
    11, 36, 20, 34, 44, 52,
]


def parse_mid(target: str) -> str | None:
    m = re.search(r"space\.bilibili\.com/(\d+)", target or "")
    if m:
        return m.group(1)
    t = (target or "").strip()
    return t if t.isdigit() else None


def wbi_sign(params: dict, img_key: str, sub_key: str, ts: int | None = None):
    """按 B站 wbi 算法签名;返回带 wts/w_rid 的新参数 dict。"""
    mixin_key = "".join((img_key + sub_key)[i] for i in _MIXIN_TAB)[:32]
    drop = str.maketrans("", "", "!'()*")  # 官方过滤字符
    signed = {k: str(v).translate(drop) for k, v in params.items() if v is not None}
    signed["wts"] = str(ts or int(time.time()))
    signed = dict(sorted(signed.items()))
    query = "&".join(
        f"{quote(k, safe='')}={quote(v, safe='')}"
        for k, v in signed.items()
    )
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return signed



def _refresh_ticket(s: requests.Session, csrf: str):
    """续签 bili_ticket(3 天有效期,过期后接口易触发 -352 风控)。"""
    import hmac

    ts = int(time.time())
    hexsign = hmac.new(b"XgwSnGZ1p", f"ts{ts}".encode(), hashlib.sha256).hexdigest()
    try:
        r = s.post(
            "https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket",
            params={"key_id": "ec02", "hexsign": hexsign,
                    "context[ts]": ts, "csrf": csrf},
            headers={"Referer": "https://www.bilibili.com/"},
            timeout=10,
        )
        body = r.json()
        data = body.get("data") or {}
        if data.get("ticket"):
            expires = data.get("ticket_expires") or (ts + 259200)
            s.cookies.set("bili_ticket", data["ticket"], domain=".bilibili.com")
            s.cookies.set("bili_ticket_expires", str(expires),
                          domain=".bilibili.com")
            logger.info("bili_ticket 已续签")
        else:
            logger.warning("bili_ticket 续签失败: %s", body.get("message"))
    except Exception as e:  # noqa: BLE001 — 续签失败仍尝试原请求
        logger.warning("bili_ticket 续签异常:%s", e)


def _session(cookies_path: str | None = None) -> requests.Session:
    """带 buvid 预热的会话;提供 cookies.txt 时注入登录态(过风控的关键)。"""
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    try:
        spi = s.get("https://api.bilibili.com/x/frontend/finger/spi",
                    timeout=10).json()["data"]
        s.cookies.set("buvid3", spi["b_3"], domain=".bilibili.com")
        s.cookies.set("buvid4", spi["b_4"], domain=".bilibili.com")
    except Exception as e:  # noqa: BLE001 — 指纹预热失败不致命
        logger.warning("buvid 预热失败:%s", e)
    if cookies_path:
        ck = BiliCookie.parse_file(cookies_path)
        for k, v in ck.items():
            s.cookies.set(k, v, domain=".bilibili.com")
        try:
            exp = int(ck.get("bili_ticket_expires") or 0)
        except (TypeError, ValueError):
            exp = 0
        if time.time() > exp - 600 and ck.get("bili_jct"):
            _refresh_ticket(s, ck["bili_jct"])
    return s


def _get_latest_wbi(s, mid: str, limit: int) -> list[dict]:
    """主通道:wbi 签名的投稿列表(风控最严)。"""
    nav = s.get("https://api.bilibili.com/x/web-interface/nav",
                timeout=10).json()["data"]["wbi_img"]
    img_key = nav["img_url"].rsplit("/", 1)[1].split(".")[0]
    sub_key = nav["sub_url"].rsplit("/", 1)[1].split(".")[0]
    params = wbi_sign({"mid": mid, "ps": limit, "tid": 0, "pn": 1,
                       "keyword": "", "order": "pubdate",
                       "platform": "web", "web_location": "1550101"},
                      img_key, sub_key)
    r = s.get("https://api.bilibili.com/x/space/wbi/arc/search",
              params=params,
              headers={"Referer": f"https://space.bilibili.com/{mid}/"},
              timeout=15)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"wbi 通道 code={body.get('code')}: {body.get('message')}")
    return [{"bvid": v["bvid"], "title": v["title"], "created": v.get("created")}
            for v in body["data"]["list"].get("vlist", [])[:limit]]


def _get_latest_series(s, mid: str, limit: int) -> list[dict]:
    """降级通道:系列检索接口(免 wbi,风控宽松,实测可用)。"""
    r = s.get("https://api.bilibili.com/x/series/recArchivesByKeywords",
              params={"mid": mid, "keywords": "", "ps": limit, "pn": 1},
              headers={"Referer": f"https://space.bilibili.com/{mid}/"},
              timeout=15)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(
            f"series 通道 code={body.get('code')}: {body.get('message')}")
    out = []
    for a in (body.get("data") or {}).get("archives", [])[:limit]:
        created = a.get("pubdate")
        out.append({"bvid": a.get("bvid"), "title": a.get("title"),
                    "created": created})
    return [v for v in out if v["bvid"]]


def get_up_latest(mid: str, limit: int = 10,
                  cookies_path: str | None = None) -> list[dict]:
    """UP主最新投稿。主通道被风控时自动降级到备用通道。"""
    s = _session(cookies_path)
    try:
        videos = _get_latest_wbi(s, mid, limit)
        if videos:
            return videos
    except Exception as e:  # noqa: BLE001 — 主通道被风控,降级
        logger.warning("wbi 通道失败(%s),降级到 series 通道", str(e)[:80])
    videos = _get_latest_series(s, mid, limit)
    logger.info("series 通道返回 %d 条", len(videos))
    return videos


def up_name(mid: str) -> str | None:
    try:
        r = requests.get("https://api.bilibili.com/x/web-interface/card",
                         params={"mid": mid, "photo": "false"},
                         headers={"User-Agent": UA}, timeout=10)
        return r.json()["data"]["card"]["name"]
    except Exception as e:  # noqa: BLE001 — 名字拿不到不影响订阅
        logger.warning("获取 UP主 名称失败:%s", e)
        return None
