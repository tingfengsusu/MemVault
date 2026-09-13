"""任务 worker:轮询 jobs 表分发执行。

step() 可单步执行(测试/面板手动重试用);run_worker() 为常驻循环。
"""
import json
import logging
import time

logger = logging.getLogger(__name__)


def build_dispatch(memory, cfg: dict) -> dict:
    from memvault.pipeline import text as text_mod
    from memvault.pipeline import video
    from memvault.pipeline import files as files_mod

    return {
        "ingest_video": lambda p: video.ingest_video(
            p["source"], memory, cfg, domain=p.get("domain", "general"),
            progress=lambda m: logger.info("[job] %s", m),
        ),
        "ingest_text": lambda p: text_mod.ingest_text(p, memory),
        "ingest_product": lambda p: text_mod.ingest_product(p, memory),
        "ingest_file": lambda p: files_mod.ingest_file(p, memory, cfg),
    }


def step(memory, cfg: dict, dispatch: dict | None = None) -> bool:
    """取一个任务并执行。返回是否执行了任务(供测试单步驱动)。"""
    dispatch = dispatch or build_dispatch(memory, cfg)
    job = memory.db.claim_next()
    if job is None:
        return False
    payload = json.loads(job["payload"])
    fn = dispatch.get(job["type"])
    logger.info("执行 job#%s %s", job["id"], job["type"])
    if fn is None:
        memory.db.finish_job(job["id"], ok=False,
                             error=f"未知任务类型: {job['type']}")
        return True
    try:
        fn(payload)
        memory.db.finish_job(job["id"], ok=True)
    except Exception as e:  # noqa: BLE001 — 单任务失败不终止 worker
        logger.exception("job#%s 失败", job["id"])
        memory.db.finish_job(job["id"], ok=False, error=str(e))
    return True


def run_worker(memory, cfg: dict, poll_seconds: float = 2.0,
               stop=None, dispatch: dict | None = None):
    """阻塞式任务循环;stop 为可调用对象,返回 True 时退出。"""
    logger.info("worker 启动,轮询间隔 %.1fs", poll_seconds)
    while not (stop and stop()):
        if not step(memory, cfg, dispatch):
            time.sleep(poll_seconds)
