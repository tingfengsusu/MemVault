"""应用技能接口(M4 启用,M1 先定协议)。

技能 = 检索策略 + 提示词 + 可选执行动作。
"""
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass
class Context:
    """技能组装的检索上下文,喂给 LLM 的全部个人数据。"""

    query: str
    items: list[dict] = field(default_factory=list)   # memory.search 结果
    profile: list[dict] = field(default_factory=list)  # 画像键值
    logs: list[dict] = field(default_factory=list)     # 时序日志


@dataclass
class Reply:
    text: str
    suggest_action: Optional[dict] = None  # 如 {"type": "jd_add_to_cart", "keyword": "..."}


class Skill(Protocol):
    domain: str

    def build_context(self, query: str, memory) -> Context: ...

    def reply(self, ctx: Context, user_input: str) -> Reply: ...

    def act(self, reply: Reply) -> Any:  # 可选执行动作(加购等)
        ...
