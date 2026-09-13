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
        for k, v in BiliCookie.parse_file(cookies_path).items():
            s.cookies.set(k, v, domain=".bilibili.com")
    return s


def get_up_latest(mid: str, limit: int = 10,
                  cookies_path: str | None = None) -> list[dict]:
    """UP主最新投稿,返回 [{bvid, title, created}]。"""
    s = _session(cookies_path)
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
    code = body.get("code")
    if code != 0:
        hint = ""
        if code in (-352, -799, -412):
            hint = (" — B站风控拦截:请在 config.yaml 设置 bili.cookies_path "
                    "指向导出的 cookies.txt(登录 B站后用浏览器扩展导出),"
                    "然后重启服务")
        raise RuntimeError(f"B站接口返回 code={code}: {body.get('message')}{hint}")
    out = []
    for v in body["data"]["list"].get("vlist", [])[:limit]:
        out.append({"bvid": v["bvid"], "title": v["title"],
                    "created": v.get("created")})
    return out


def up_name(mid: str) -> str | None:
    try:
        r = requests.get("https://api.bilibili.com/x/web-interface/card",
                         params={"mid": mid, "photo": "false"},
                         headers={"User-Agent": UA}, timeout=10)
        return r.json()["data"]["card"]["name"]
    except Exception as e:  # noqa: BLE001 — 名字拿不到不影响订阅
        logger.warning("获取 UP主 名称失败:%s", e)
        return None
