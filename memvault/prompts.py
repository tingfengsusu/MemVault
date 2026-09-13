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
3. 置信度不足或树中确无合适分类时,给出 new_category 提议(name 简短中文),否则为 null。
4. reason 用一句话说明判断依据。

输出 JSON:
{"category_id": 整数或null, "new_category": {"name": "..."}或null, "confidence": 0到1的小数, "reason": "..."}"""

EXTRACT_PROMPT = """你是信息提取器。从条目内容中提取分类「{category}」的结构化属性。

规则:
1. 只输出一个 JSON 对象,键为属性名(中文),值为字符串或 null。
2. 内容中没有的信息输出 null,禁止编造。
3. 只提取与该分类相关的属性;价格保留原文写法。
4. 内容可能来自视频(有语音/画面文字)或网页,缺哪种模态就忽略哪种,不要因此拒绝提取。"""


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
        if not self.get_active("extract", "extract", None):
            self.new_version("extract", "extract",
                             EXTRACT_PROMPT.replace("{category}", "通用"))

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
