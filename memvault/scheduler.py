"""订阅调度:watch_sources 定时检查 → 新内容生成采集任务。

worker 通过 watch_check 任务执行检查;"首次检查只登记历史"防止
订阅瞬间把 UP主 全部旧视频灌进库。
"""
import logging
import time

logger = logging.getLogger(__name__)


def check_source(memory, cfg, source_id: int, fetch=None) -> dict:
    """检查一个订阅源,返回 {new, seeded, total}。fetch 可注入(测试)。"""
    from memvault.sources import bili_watch

    src = memory.db.get_watch_source(source_id)
    if not src:
        raise ValueError(f"订阅源不存在: {source_id}")
    if src["kind"] != "bili_up":
        raise ValueError(f"暂不支持的订阅类型: {src['kind']}")

    mid = bili_watch.parse_mid(src["target"])
    if not mid:
        raise ValueError(f"无法从 target 解析 UP主 UID: {src['target']}")

    limit = cfg.get("watch", {}).get("max_per_check", 10)
    cookies_path = cfg.get("bili", {}).get("cookies_path")
    fetch = fetch or (lambda m, n: bili_watch.get_up_latest(m, n,
                                                            cookies_path=cookies_path))
    videos = fetch(mid, limit)
    first_run = src.get("last_checked") is None
    new = seeded = 0
    for v in videos:
        key = f"bili|{v['bvid']}"
        if first_run:
            # 历史视频登记为已完成任务,堵住后续去重通道
            if not memory.db.job_exists(key):
                jid = memory.db.enqueue(
                    "ingest_video",
                    {"source": v["bvid"], "domain": src["domain"] or "general"},
                    dedup_key=key)
                memory.db.finish_job(jid, ok=True)
                seeded += 1
            continue
        if not memory.db.job_exists(key):
            memory.db.enqueue(
                "ingest_video",
                {"source": v["bvid"], "domain": src["domain"] or "general"},
                dedup_key=key)
            new += 1
    memory.db.touch_watch_source(source_id)
    logger.info("订阅源#%s(%s):新 %d,首次登记 %d,共 %d 条",
                source_id, mid, new, seeded, len(videos))
    return {"new": new, "seeded": seeded, "total": len(videos)}


def schedule_pass(memory, cfg, interval_seconds: int) -> int:
    """给所有启用的订阅源排一个本轮检查任务(时间桶去重)。返回本轮排入数。"""
    bucket = int(time.time() // interval_seconds)
    n = 0
    for src in memory.db.watch_sources(enabled_only=True):
        before = memory.db.job_exists(
            f"watch|{src['id']}|{bucket}")
        jid = memory.db.enqueue(
            "watch_check", {"source_id": src["id"]},
            dedup_key=f"watch|{src['id']}|{bucket}")
        if not before:
            n += 1
    return n


def run_scheduler(memory, cfg, stop=None, interval_minutes=None):
    """常驻循环:每 interval 给启用源排检查任务(worker 真正执行)。"""
    interval = int((interval_minutes
                    or cfg.get("watch", {}).get("interval_minutes", 30)) * 60)
    logger.info("订阅调度启动,间隔 %d 分钟", interval // 60)
    while not (stop and stop()):
        try:
            schedule_pass(memory, cfg, interval)
        except Exception as e:  # noqa: BLE001 — 调度失败不终止常驻
            logger.exception("订阅调度失败:%s", e)
        for _ in range(0, interval, 5):
            if stop and stop():
                return
            time.sleep(5)
