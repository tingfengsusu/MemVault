"""任务 worker:轮询 jobs 表分发执行(M2 托盘常驻复用,M1 仅供测试)。"""
import json
import logging
import time

logger = logging.getLogger(__name__)


def make_dispatch(memory, cfg: dict) -> dict:
    from memvault.pipeline import video

    return {
        "ingest_video": lambda payload: video.ingest_video(
            payload["source"], memory, cfg,
            domain=payload.get("domain", "general"),
            progress=lambda m: logger.info("[job] %s", m),
        ),
    }


def run_worker(memory, cfg: dict, poll_seconds: float = 2.0,
               stop=None, dispatch: dict | None = None):
    """阻塞式任务循环;stop 为可调用对象,返回 True 时退出。"""
    dispatch = dispatch or make_dispatch(memory, cfg)
    logger.info("worker 启动,轮询间隔 %.1fs", poll_seconds)
    while not (stop and stop()):
        job = memory.db.claim_next()
        if job is None:
            time.sleep(poll_seconds)
            continue
        payload = json.loads(job["payload"])
        fn = dispatch.get(job["type"])
        logger.info("执行 job#%s %s", job["id"], job["type"])
        if fn is None:
            memory.db.finish_job(job["id"], ok=False,
                                 error=f"未知任务类型: {job['type']}")
            continue
        try:
            fn(payload)
            memory.db.finish_job(job["id"], ok=True)
        except Exception as e:  # noqa: BLE001 — 单任务失败不终止 worker
            logger.exception("job#%s 失败", job["id"])
            memory.db.finish_job(job["id"], ok=False, error=str(e))
