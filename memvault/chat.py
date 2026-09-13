"""对话式录入与问答:聊天页的后端。

一轮 = 抽取(画像/日志/检索词)→ 检索个人上下文 → 生成回复。
画像与日志持久化;对话本身不落盘(M4 技能层再增强)。
"""
import logging

logger = logging.getLogger(__name__)

EXTRACT_SYSTEM = """你是个人记忆库的信息抽取器。从用户消息中抽取三类信息:
1. profile:关于用户的长期事实(身高/体重/目标/偏好/尺码等),输出 [{"key","value"}],没有为 []
2. log:用户报告的已完成行为或事件(如"今天练了胸5组"),输出 {"content","domain"} 或 null
   domain 按主题判断:健身/购物/烹饪等,不确定用 general
3. search_query:用户在提问或寻求建议时,给一个适合语义检索的中文查询串;纯陈述没有为 null
输出 JSON:{"profile": [...], "log": {...}|null, "search_query": "...|null"}"""

REPLY_SYSTEMS = {
    "general": (
        "你是 MemVault 个人记忆库助手。基于「个人上下文」回答问题;"
        "上下文来自用户自己的库(条目/画像/日志),与问题无关的部分忽略。"
        "回答用简洁中文。"
    ),
    "fitness": (
        "你是用户的私人健身教练。结合「训练日志」判断近期训练量是否失衡,"
        "结合「画像」给出个性化建议(明确指出:什么练多了、明天建议练什么部位、注意事项)。"
        "用户刚报告了今天的训练时,先确认已记录,再给建议。用友好专业的中文。"
    ),
    "shopping": (
        "你是用户的购物决策助手。结合「个人上下文」中已收藏/已有的商品,"
        "提示重复购买、类似款、搭配建议;不确定的信息明说不确定。用简洁中文。"
    ),
}


def _context_text(memory, skill: str, results: list[dict]) -> str:
    parts = []
    profile = memory.get_profile()
    if profile:
        lines = [f"- {p['domain']}/{p['key']}: {p['value_json']}"
                 for p in profile]
        parts.append("画像:\n" + "\n".join(lines))
    logs = memory.timeline(days=7, domain=skill if skill in ("fitness", "shopping") else None)
    if logs:
        lines = [f"- {l['happened_at']} {l['content_text']}" for l in logs[:20]]
        parts.append("近期日志:\n" + "\n".join(lines))
    if results:
        lines = []
        for r in results:
            c = r["chunk"]
            snippet = (c.get("content") or "")[:120].replace("\n", " ")
            lines.append(f"- [{r['item']['title']}] {snippet}")
        parts.append("相关条目:\n" + "\n".join(lines))
    return "\n\n".join(parts) if parts else "(暂无个人上下文)"


def chat_turn(memory, llm, message: str, skill: str = "general") -> dict:
    """处理一条用户消息,返回 {reply, profile_saved, log_saved, context_used}。"""
    if not llm.enabled:
        raise RuntimeError("LLM 未配置")
    if not message.strip():
        raise ValueError("消息为空")

    extraction = llm.chat_json(EXTRACT_SYSTEM, message)

    saved_profile = []
    for p in extraction.get("profile") or []:
        if p.get("key") and p.get("value") is not None:
            memory.set_profile(p["key"], p["value"],
                               domain=skill if skill != "general" else "general",
                               source="chat")
            saved_profile.append(p["key"])

    log_saved = None
    lg = extraction.get("log")
    if isinstance(lg, dict) and lg.get("content"):
        memory.add_log(
            lg["content"],
            domain=lg.get("domain") or (skill if skill != "general" else None),
            source="chat")
        log_saved = lg["content"]

    q = extraction.get("search_query")
    results = memory.search(q, top_k=6) if q else []
    context = _context_text(memory, skill, results)

    system = REPLY_SYSTEMS.get(skill, REPLY_SYSTEMS["general"])
    user = f"个人上下文:\n{context}\n\n用户消息:{message}"
    reply = llm.chat(system, user)
    return {"reply": reply, "profile_saved": saved_profile,
            "log_saved": log_saved, "context_used": len(results)}
