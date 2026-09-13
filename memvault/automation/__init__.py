"""执行动作层:购物技能的加购执行(移植自 Video2Shop JdHandler)。

安全边界:加购只由用户在面板显式点击触发(入 jd_cart 队列),
绝不由 LLM/对话自动执行。
"""
import logging

logger = logging.getLogger(__name__)


def jd_cart_add(payload: dict, handler_factory=None) -> dict:
    """执行一次京东搜索加购。阻塞且可能拉起浏览器,只能跑在 worker。"""
    from memvault.automation.jd import JdHandler

    keyword = payload["keyword"]
    factory = handler_factory or JdHandler
    h = factory()
    try:
        h.login()  # 登录态无效时会等用户在 Chrome 里手动登录(5 分钟超时)
        ok = h.search_and_add(keyword)
    finally:
        h.close()
    if not ok:
        raise RuntimeError(f"京东加购失败: {keyword}")
    logger.info("加购成功: %s", keyword)
    return {"keyword": keyword, "ok": True}
