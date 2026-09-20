"""auto_process:采集完成后的 LLM 自动处理(路由→提取)。

LLM 未配置时静默跳过(条目留在待整理箱),不视为任务失败。
"""
import logging

logger = logging.getLogger(__name__)


def auto_process(payload: dict, memory, cfg: dict, llm=None,
                 pstore=None) -> dict:
    from memvault.classify import extract_item, route_item
    from memvault.llm import get_llm_client
    from memvault.prompts import PromptStore

    llm = llm or get_llm_client(cfg)
    pstore = pstore or PromptStore(memory.db)
    item_id = payload["item_id"]

    # 同源重跑会先删旧条目再建新条目,旧条目上排队的任务此时已无意义
    if memory.get_item(item_id) is None:
        logger.info("item=%s 已不存在(同源重跑清理),跳过自动处理", item_id)
        return {"action": "gone"}

    if not llm.enabled:
        memory.db.set_item_category(item_id, None, None, "LLM 未配置,待手动归类")
        logger.info("item=%s LLM 未配置,跳过自动分类", item_id)
        return {"action": "skipped"}

    pstore.ensure_seed()
    result = route_item(memory, llm, pstore, item_id, cfg)
    # 无论路由结果如何都提取属性:留箱条目也要有可读的内容摘要(M3 缺陷修复)
    try:
        extract_item(memory, llm, pstore, item_id, cfg)
    except Exception as e:  # noqa: BLE001 — 提取失败不影响已完成的分类
        logger.warning("item=%s 属性提取失败:%s", item_id, e)

    # 关键片段:命中提示词规则(购物向)且有结构单元的条目才跑 —— 独立的小 schema 调用
    try:
        from memvault.classify import extract_clips
        from memvault.prompts import resolve_prompt_name

        item = memory.get_item(item_id)
        if item and resolve_prompt_name(memory, item, stage="extract"):
            n = len(extract_clips(memory, llm, pstore, item_id, cfg))
            if n:
                logger.info("item=%s 关键片段 %d 段", item_id, n)
    except Exception as e:  # noqa: BLE001 — 片段失败不影响分类与提取
        logger.warning("item=%s 关键片段失败:%s", item_id, e)
    return result
