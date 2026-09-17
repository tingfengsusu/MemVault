"""把 B站收藏夹的全部视频加入采集队列(worker 逐个处理)。

用法:
  .venv/Scripts/python scripts/ingest_favlist.py <收藏夹URL> [--domain general]

收藏夹 URL 形如 https://space.bilibili.com/<mid>/favlist?fid=<media_id>
113 条视频约需数小时(下载+ASR 逐个进行),任务进度见面板"任务"页。
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memvault.config import db_path, load_config  # noqa: E402
from memvault.db import Database  # noqa: E402
from memvault.sources.bili_watch import _session  # noqa: E402


def fetch_all(media_id: str, mid: str, cookies_path: str) -> list[dict]:
    s = _session(cookies_path)
    out, pn = [], 1
    while True:
        r = s.get(
            "https://api.bilibili.com/x/v3/fav/resource/list",
            params={"media_id": media_id, "pn": pn, "ps": 20,
                    "order": "mtime", "type": 0, "platform": "web"},
            headers={"Referer": f"https://space.bilibili.com/{mid}/"
                                f"favlist?fid={media_id}"},
            timeout=15)
        body = r.json()
        if body.get("code") != 0:
            raise RuntimeError(f"收藏夹接口失败 code={body.get('code')}: "
                               f"{body.get('message')}")
        data = body.get("data") or {}
        medias = data.get("medias") or []
        out.extend(medias)
        info = data.get("info") or {}
        print(f"  第 {pn} 页: {len(medias)} 条(收藏夹共 {info.get('media_count')} 条)")
        if not data.get("has_more"):
            break
        pn += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="收藏夹页面 URL(含 fid=)")
    ap.add_argument("--domain", default="general")
    args = ap.parse_args()

    m = re.search(r"space\.bilibili\.com/(\d+)", args.url)
    fid = re.search(r"fid=(\d+)", args.url)
    if not fid:
        print("URL 中未找到 fid 参数", file=sys.stderr)
        sys.exit(2)
    mid = m.group(1) if m else "0"

    cfg = load_config()
    cookies = str(cfg.get("bili", {}).get("cookies_path") or "")
    db = Database(db_path(cfg))

    print(f"抓取收藏夹 fid={fid.group(1)} ...")
    medias = fetch_all(fid.group(1), mid, cookies)

    videos = [x for x in medias if x.get("type") == 2 and x.get("bvid")]
    skipped_type = len(medias) - len(videos)
    new = existed = 0
    for v in videos:
        key = f"bili|{v['bvid']}"
        if db.job_exists(key):
            existed += 1
            continue
        db.enqueue("ingest_video",
                   {"source": v["bvid"], "domain": args.domain},
                   dedup_key=key)
        new += 1
    print(f"完成: 收藏夹 {len(medias)} 条(非视频 {skipped_type} 条跳过) | "
          f"新入队 {new} | 已在队列/已处理 {existed}")
    print("worker 将逐个处理;启动服务(tray)后进度见面板'任务'页。")


if __name__ == "__main__":
    main()
