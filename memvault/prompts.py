"""提示词系统:版本化存储 + 质疑驱动的进化(DESIGN §13.1)。

- 提示词存 DB 带版本,active 唯一,可回滚(改 status 即可)。
- 用户质疑 → 检索相似历史质疑(feedback 向量库)→ LLM 重写 → 新版本(origin=feedback)。
"""
import logging
import uuid

logger = logging.getLogger(__name__)

ROUTER_PROMPT = """你是个人记忆库的分类路由器。把条目分到用户分类树中的某个分类。

规则:
1. 只能从"分类树"给出的分类中选择(category_id 必须来自树中)。
2. 参考相似条目(该用户库中已分类的近邻)辅助判断。
3. 树中确有合适分类且置信度足够时才给 category_id;若树中没有合适分类,
   **必须在 new_category 给出提议**(简短中文分类名),不要只在 reason 里描述
   而把提议留空;仅当内容完全无法归类时才让 new_category 为 null。
4. reason 用一句话说明判断依据。

输出 JSON:
{"category_id": 整数或null, "new_category": {"name": "..."}或null, "confidence": 0到1的小数, "reason": "..."}"""

EXTRACT_PROMPT_V1 = """你是信息提取器。为分类「{category}」的条目提取结构化属性。

步骤:
1. 阅读条目内容,归纳出 3~6 个对该类条目最有价值的属性名(中文,例如:主题/核心内容/关键要点/方法/对象/风格)。
2. 从内容中提取每个属性的值。属性的值应当具体、包含实际信息(例如关键结论、数字、名称、步骤),不要只写空泛概括。

规则:
- 只输出一个 JSON 对象:键为属性名,值为字符串或 null。
- 内容中没有的信息输出 null,禁止编造。
- 禁止输出与属性无关的键(例如 type、response_format)。
- 内容可能来自视频(语音转写/画面文字)或网页;语音转写可能是不连贯的短句,
  请综合全文归纳要点,不要因为语句零碎而拒绝提取。"""

# 相对 v1 的改进(用户实测 #14 反馈):①按内容形态给属性名的引导 ②并列对象必须逐个展开
# ③数值/步骤保留原样 ④广告内容按用户设置处理(占位符由运行期注入)
EXTRACT_PROMPT = """你是信息提取器。为分类「{category}」的条目提取结构化属性。

步骤:
1. 先判断内容形态,再定属性名(4~8 个,中文):
   - 教学/做法/教程类(做菜、健身动作、软件操作、手工…):按"对象 + 步骤"拆,
     例如 食材与用量 / 制作步骤 / 时间与温度 / 关键技巧 / 常见失败原因 / 适用对象;
   - 评测/资讯/解读类:例如 核心结论 / 关键数据 / 论据与案例 / 影响与后续 / 局限;
   - 商品类:例如 价格与规格 / 卖点 / 适用场景 / 与同类差异。
2. 逐条写具体信息。凡是内容里出现的**数字、比例、时长、温度、型号、价格、名称、
   顺序**都要保留原样(例:"400ml 牛奶 + 2 勺糖,冷冻 20 分钟"),不要概括成
   "若干""适量""多个步骤"。
3. 内容里有**多个并列对象**时(多道菜 / 多款商品 / 多个方法 / 多期内容),必须逐个
   展开,每个对象单独列出它自己的关键信息与步骤,不要只写一句共同点。
   - 例:一段"12 个冰淇淋做法"的视频 → 属性里要能看到每款的做法差异
     (用了什么水果/配料、怎么处理、冻多久),而不是只说"多种创意做法"。

规则:
- 只输出一个 JSON 对象:键为属性名,值为字符串或 null。
- 内容中没有的信息输出 null,禁止编造;信息确实琐碎时宁可少写属性,也不要空泛填充。
- 禁止输出与属性无关的键(例如 type、response_format)。
- 内容可能来自视频(语音转写/画面文字)或网页;语音转写与 OCR 文字可能零碎、
  有错别字或串行,请综合全文归纳,不要因为语句零碎而拒绝提取。
- {ads_policy}"""


class PromptStore:
    def __init__(self, db):
        self.db = db

    # ── 版本管理 ──────────────────────────────────────────────────────
    def new_version(self, name, stage, content, category_id=None,
                    origin="system", status="active") -> int:
        conn = self.db._conn()
        if status == "active":
            conn.execute(
                "UPDATE prompts SET status='retired' WHERE name=? AND stage=?"
                " AND category_id IS ? AND status='active'",
                (name, stage, category_id),
            )
        ver = conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM prompts"
            " WHERE name=? AND stage=? AND category_id IS ?",
            (name, stage, category_id),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO prompts(name, stage, category_id, version, content,"
            " status, origin) VALUES(?,?,?,?,?,?,?)",
            (name, stage, category_id, ver, content, status, origin),
        )
        conn.commit()
        logger.info("提示词 %s/%s 新版本 v%s(origin=%s)", name, stage, ver, origin)
        return cur.lastrowid

    def get_active(self, name, stage, category_id=None) -> dict | None:
        return self.db.active_prompt(name, stage, category_id)

    def versions(self, name, stage, category_id=None) -> list[dict]:
        rows = self.db._conn().execute(
            "SELECT * FROM prompts WHERE name=? AND stage=? AND category_id IS ?"
            " ORDER BY version DESC", (name, stage, category_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def rollback(self, prompt_id: int):
        """回滚:把指定版本设为 active,其余退役。"""
        row = self.db._conn().execute(
            "SELECT * FROM prompts WHERE id=?", (prompt_id,)
        ).fetchone()
        if not row:
            return
        self.new_version(row["name"], row["stage"], row["content"],
                         category_id=row["category_id"], origin="rollback")

    # ── 种子提示词 ────────────────────────────────────────────────────
    def ensure_seed(self):
        if not self.get_active("router", "classify"):
            self.new_version("router", "classify", ROUTER_PROMPT)
        active = self.get_active("extract", "extract", None)
        if active is None:
            # 保留 {category} / {ads_policy} 占位符,运行期再替换
            self.new_version("extract", "extract", EXTRACT_PROMPT)
            return
        # 出厂提示词跟随升级:只有当 active 仍是"未被改写的出厂版本"时才替换
        # (用户/LLM 改写过的版本一律保留,符合"提示词可进化 + 可回滚"的设计)
        cur = (active.get("content") or "").strip()
        factory = {EXTRACT_PROMPT_V1.strip(),
                   EXTRACT_PROMPT_V1.replace("{category}", "通用").strip()}
        if cur in factory:
            self.new_version("extract", "extract", EXTRACT_PROMPT,
                             origin="upgrade")
            logger.info("出厂提取提示词升级(旧版已退役,可回滚)")

    # ── 质疑(feedback)──────────────────────────────────────────────
    def add_feedback(self, memory, prompt_id: int, item_id: int | None,
                     critique: str, dimension: str | None = None,
                     category_id: int | None = None) -> int:
        conn = self.db._conn()
        cur = conn.execute(
            "INSERT INTO prompt_feedback(prompt_id, item_id, critique, dimension)"
            " VALUES(?,?,?,?)", (prompt_id, item_id, critique, dimension),
        )
        conn.commit()
        fb_id = cur.lastrowid
        try:
            vec = memory.embedder.encode([critique])[0]
            meta = {"prompt_id": prompt_id, "item_id": item_id or 0,
                    "dimension": dimension or "",
                    "category_id": category_id}
            meta = {k: v for k, v in meta.items() if v is not None}
            memory.vs.upsert_feedback([fb_id], [vec], [critique], [meta])
        except Exception as e:  # noqa: BLE001 — 向量化失败不影响质疑落库
            logger.warning("质疑向量化失败:%s", e)
        return fb_id

    def similar_critiques(self, memory, category_id: int | None,
                          query_text: str, k: int = 3) -> list[str]:
        """检索同分类下语义最相近的历史质疑。"""
        try:
            vec = memory.embedder.encode([query_text])[0]
            where = {"category_id": category_id} if category_id is not None else None
            ids = memory.vs.query_feedback(vec, where=where, k=k)
            if not ids:
                return []
            marks = ",".join("?" * len(ids))
            rows = self.db._conn().execute(
                f"SELECT critique FROM prompt_feedback WHERE id IN ({marks})", ids
            ).fetchall()
            return [r["critique"] for r in rows]
        except Exception as e:  # noqa: BLE001
            logger.warning("相似质疑检索失败:%s", e)
            return []

    # ── 进化:质疑 → 重写(流程 C,单次 LLM 调用版)───────────────────
    REWRITE_SYSTEM = (
        "你是提示词工程师。用户对一条自动提取结果提出了质疑。"
        "请改写提取提示词,使其今后能避免该问题。"
        "保留原提示词中仍然正确的规则;修改要具体、可执行;只输出 JSON:"
        '{"prompt": "改写后的完整提示词", "changes": "一句话说明改了什么",'
        ' "dimension": "质疑指向的维度,如:价格格式/尺码/字段缺失"}'
    )

    def rewrite_from_feedback(self, memory, llm, name, stage, category_id,
                              item: dict, critique: str) -> dict:
        current = self.get_active(name, stage, category_id)
        if current is None and category_id is not None:
            # 该分类还没有专属提示词:从通用版分叉出 v1,再在其上改写
            generic = self.get_active(name, stage, None)
            self.new_version(name, stage,
                             generic["content"] if generic else "",
                             category_id=category_id, origin="system")
            current = self.get_active(name, stage, category_id)
        if current is None:
            raise ValueError(f"无活跃提示词 {name}/{stage}")
        history = self.similar_critiques(memory, category_id, critique)
        user = (
            f"当前提示词:\n{current['content']}\n\n"
            f"出错条目标题:{item.get('title')}\n"
            f"条目内容:{(item.get('content_text') or '')[:1200]}\n"
            f"条目属性:{item.get('attrs_json')}\n\n"
            f"用户质疑:{critique}\n\n"
            + (f"历史相似质疑(合并吸取教训):\n- " + "\n- ".join(history)
               if history else "无历史质疑。")
        )
        resp = llm.chat_json(self.REWRITE_SYSTEM, user)
        new_id = self.new_version(name, stage, resp["prompt"],
                                  category_id=category_id, origin="feedback")
        self.add_feedback(memory, current["id"], item.get("id"), critique,
                          dimension=resp.get("dimension"),
                          category_id=category_id)
        return {"prompt_id": new_id, "version_note": resp.get("changes", "")}
