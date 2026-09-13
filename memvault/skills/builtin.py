"""内置应用技能:每个技能 = 检索策略 + 提示词(+ 可选执行动作)。

新增领域 = 加一个 Skill 子类并注册进 SKILLS,平台零改动(DESIGN §7)。
"""
from dataclasses import dataclass


@dataclass
class SkillSpec:
    key: str
    title: str
    system_prompt: str
    timeline_domain: str | None = None    # 日志过滤;None = 全部
    timeline_days: int = 7
    search_kwargs: dict | None = None     # memory.search 额外过滤(如 domain)
    search_top_k: int = 6


GENERAL = SkillSpec(
    key="general",
    title="通用助手",
    system_prompt=(
        "你是 MemVault 个人记忆库助手。基于「个人上下文」回答问题;"
        "上下文来自用户自己的库(条目/画像/日志),与问题无关的部分忽略。"
        "回答用简洁中文。"
    ),
)

FITNESS = SkillSpec(
    key="fitness",
    title="健身教练",
    system_prompt=(
        "你是用户的私人健身教练。结合「训练日志」判断近期训练量是否失衡,"
        "结合「画像」给出个性化建议(明确指出:什么练多了、明天建议练什么部位、注意事项)。"
        "用户刚报告了今天的训练时,先确认已记录,再给建议。用友好专业的中文。"
    ),
    timeline_domain="fitness",
    timeline_days=14,
    search_kwargs={"domain": "fitness"},
    search_top_k=5,
)

SHOPPING = SkillSpec(
    key="shopping",
    title="购物决策",
    system_prompt=(
        "你是用户的购物决策助手。结合「个人上下文」中已收藏/已有的商品,"
        "提示重复购买、类似款、搭配建议;不确定的信息明说不确定。用简洁中文。"
    ),
    timeline_domain="shopping",
    search_kwargs={"domain": "shopping"},
    search_top_k=5,
)

SKILLS: dict[str, SkillSpec] = {s.key: s for s in (GENERAL, FITNESS, SHOPPING)}


def get_skill(key: str | None) -> SkillSpec:
    return SKILLS.get(key or "general", GENERAL)
