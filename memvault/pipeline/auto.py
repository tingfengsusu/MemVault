"""auto_process:采集完成后的 LLM 自动处理(路由→提取)。

LLM 未配置时静默跳过(条目留在待整理箱),不视为任务失败。
"""
import logging

logger = logging.getLogger(__name__)


def auto_process(payload: dict, memory, cfg: dict, llm=None,
                 pstore=None) -> dict:
    from memvault.classify import extract_item, route_item
    from memvault.llm import LLMClient
    from memvault.prompts import PromptStore

    llm = llm or LLMClient(cfg)
    pstore = pstore or PromptStore(memory.db)
    item_id = payload["item_id"]

    if not llm.enabled:
        memory.db.set_item_category(item_id, None, None, "LLM 未配置,待手动归类")
        logger.info("item=%s LLM 未配置,跳过自动分类", item_id)
        return {"action": "skipped"}

    pstore.ensure_seed()
    result = route_item(memory, llm, pstore, item_id, cfg)
    # 无论路由结果如何都提取属性:留箱条目也要有可读的内容摘要(M3 缺陷修复)
    try:
        extract_item(memory, llm, pstore, item_id)
    except Exception as e:  # noqa: BLE001 — 提取失败不影响已完成的分类
        logger.warning("item=%s 属性提取失败:%s", item_id, e)
    return result
