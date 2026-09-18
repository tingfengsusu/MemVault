"""M3a:LLM 自动分类 + 提示词进化。LLM 一律注入 Stub,绝不打真 API。"""
from pathlib import Path

import pytest

from memvault.classify import extract_item, route_item
from memvault.pipeline.auto import auto_process
from memvault.prompts import PromptStore


class StubLLM:
    enabled = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat_json(self, system, user, **kw):
        self.calls.append((system, user))
        return self.responses.pop(0)


@pytest.fixture()
def pstore(memory):
    ps = PromptStore(memory.db)
    ps.ensure_seed()
    return ps


def test_route_files_item(memory, pstore, cfg):
    cat_id = memory.db.add_category("fitness", "胸部训练")
    item_id = memory.add_item("fitness", "page", "卧推计划分享",
                              content_text="今天分享我的平板卧推计划")
    llm = StubLLM([{"category_id": cat_id, "new_category": None,
                    "confidence": 0.9, "reason": "讲卧推"}])
    result = route_item(memory, llm, pstore, item_id, cfg)
    assert result["action"] == "filed"
    item = memory.get_item(item_id)
    assert item["category_id"] == cat_id
    assert item["status"] == "filed"
    assert item["category_conf"] == 0.9


def test_route_low_confidence_stays_inbox(memory, pstore, cfg):
    item_id = memory.add_item("general", "page", "随拍",
                              content_text="一些不明确的内容")
    llm = StubLLM([{"category_id": None, "new_category": None,
                    "confidence": 0.4, "reason": "看不出主题"}])
    result = route_item(memory, llm, pstore, item_id, cfg)
    assert result["action"] == "inbox"
    item = memory.get_item(item_id)
    assert item["status"] == "inbox"
    assert "未分类" in item["auto_note"]


def test_route_proposes_new_category(memory, pstore, cfg):
    item_id = memory.add_item("fitness", "page", "跑步配速",
                              content_text="今天聊马拉松配速策略")
    llm = StubLLM([{"category_id": None,
                    "new_category": {"name": "跑步"},
                    "confidence": 0.85, "reason": "讲跑步"}])
    result = route_item(memory, llm, pstore, item_id, cfg)
    assert result["action"] == "proposed"
    cat = memory.db.get_category(result["category_id"])
    assert cat["name"] == "跑步" and cat["status"] == "proposed"
    assert memory.get_item(item_id)["status"] == "inbox"  # 确认前不转 filed

    memory.db.confirm_category(cat["id"])
    assert memory.db.get_category(cat["id"])["status"] == "active"


def test_extract_updates_attrs(memory, pstore):
    cat_id = memory.db.add_category("cooking", "家常菜")
    item_id = memory.add_item("cooking", "video", "红烧肉",
                              content_text="锅里放两勺生抽,小火炖四十分钟",
                              category_id=cat_id)
    llm = StubLLM([{"食材": "生抽", "用量": None}])
    result = extract_item(memory, llm, pstore, item_id)
    import json

    attrs = json.loads(memory.get_item(item_id)["attrs_ai"])
    assert attrs["食材"] == "生抽"
    assert "用量" not in attrs  # null 字段不落库
    assert result["category"] == "家常菜"


def test_prompt_versioning_and_rollback(pstore):
    pstore.new_version("extract", "extract", "第二版内容")
    active = pstore.get_active("extract", "extract")
    assert active["content"] == "第二版内容"
    assert active["version"] == 2
    vers = pstore.versions("extract", "extract")
    assert len(vers) == 2
    assert vers[1]["status"] == "retired"  # 旧版退役但保留

    pstore.rollback(vers[1]["id"])
    assert pstore.get_active("extract", "extract")["version"] == 3
    assert pstore.get_active("extract", "extract")["origin"] == "rollback"


def test_rewrite_from_feedback(memory, pstore):
    cat_id = memory.db.add_category("shopping", "衣服")
    item_id = memory.add_item("shopping", "product", "某外套",
                              content_text="价格 199 元", category_id=cat_id)
    llm = StubLLM([{"prompt": "改写后的提示词:价格必须保留币种与原格式",
                    "changes": "新增价格格式规则",
                    "dimension": "价格格式"}])
    item = memory.get_item(item_id)
    result = pstore.rewrite_from_feedback(
        memory, llm, "extract", "extract", cat_id, item,
        "价格提取丢了'元'字,而且没区分活动价")
    assert result["prompt_id"] > 0
    active = pstore.get_active("extract", "extract", cat_id)
    assert "币种" in active["content"]
    assert active["origin"] == "feedback"
    # 质疑已入库且可被语义召回(fake 嵌入:同文本同向量)
    hits = pstore.similar_critiques(memory, cat_id,
                                    "价格提取丢了'元'字,而且没区分活动价")
    assert any("价格" in h for h in hits)


def test_auto_process_skips_without_key(memory, cfg, tmp_path, monkeypatch):
    from memvault import llm as llm_mod

    monkeypatch.setattr(llm_mod, "_VIDEO2SHOP_CONFIG", tmp_path / "no.yaml")
    monkeypatch.setattr(llm_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg["llm"]["api_key"] = None
    client = llm_mod.LLMClient(cfg)
    assert client.enabled is False

    item_id = memory.add_item("general", "page", "t", content_text="x")
    result = auto_process({"item_id": item_id}, memory, cfg, llm=client)
    assert result["action"] == "skipped"
    assert "LLM 未配置" in memory.get_item(item_id)["auto_note"]


def test_ingest_enqueues_auto_process(memory, cfg):
    from memvault.worker import build_dispatch, step

    seen = []
    dispatch = build_dispatch(memory, cfg)
    dispatch["auto_process"] = lambda p: seen.append(p["item_id"])
    memory.db.enqueue("ingest_text", {"title": "测试条目", "text": "一些内容文本"})
    assert step(memory, cfg, dispatch=dispatch) is True
    assert step(memory, cfg, dispatch=dispatch) is True  # 第二轮执行 auto_process
    assert seen == [1]


# ── 面板:分类管理 ────────────────────────────────────────────────────
@pytest.fixture()
def api_client(tmp_path):
    from fastapi.testclient import TestClient

    from memvault.config import load_config
    from memvault.server.app import create_app

    cfg = load_config()
    cfg["data_dir"] = str(tmp_path)
    cfg["embedding"]["fake"] = True
    cfg["llm"]["api_key"] = None
    app = create_app(cfg)
    with TestClient(app) as client:
        yield client, app


def test_panel_categories_flow(api_client):
    """分类页迁到 Vue 后:外壳 + /api/categories* 接口(旧表单端点双轨保留)。"""
    client, app = api_client
    page = client.get("/categories")
    assert page.status_code == 200 and 'id="categories-app"' in page.text

    # 旧表单端点仍可用
    r = client.post("/categories/add",
                    data={"domain": "fitness", "name": "胸部"},
                    follow_redirects=False)
    assert r.status_code == 303
    # 新 JSON 端点
    r2 = client.post("/api/categories", json={"domain": "fitness", "name": "背部"})
    assert r2.json()["data"]["name"] == "背部"

    data = client.get("/api/categories").json()["data"]
    names = [c["name"] for c in data["groups"]["fitness"]]
    assert "胸部" in names and "背部" in names
    assert "fitness" in data["domains"]

    # 模拟 LLM 提议的分类 → 确认采纳(接口版)
    cat_id = app.state.memory.db.add_category("fitness", "腿部",
                                              status="proposed")
    data = client.get("/api/categories").json()["data"]
    leg = next(c for c in data["items"] if c["id"] == cat_id)
    assert leg["status"] == "proposed"                       # 前端据此显示"待确认"
    assert client.post(f"/api/categories/{cat_id}/confirm").json()["ok"] is True
    assert app.state.memory.db.get_category(cat_id)["status"] == "active"

    # 删除分类(接口):条目退回、分类消失
    item = app.state.memory.add_item("fitness", "note", "待退回",
                                     category_id=cat_id, status="filed")
    d = client.post(f"/api/categories/{cat_id}/delete").json()["data"]
    assert d["items"] == 1 and app.state.memory.get_item(item)["status"] == "inbox"
    assert app.state.memory.db.get_category(cat_id) is None
    assert client.post("/api/categories/9999/delete").status_code == 404


def test_panel_category_domain_suggestions(api_client):
    """领域输入框要带已有领域下拉建议(不用每次手打全名)。"""
    client, app = api_client
    app.state.memory.add_item("general", "video", "某某视频")
    app.state.memory.add_item("reading", "doc", "某本书")
    app.state.memory.db.add_category("fitness", "胸部")

    # 领域建议由接口提供(datalist 在 Vue 组件里)
    page = client.get("/categories").text
    assert 'id="categories-app"' in page
    data = client.get("/api/categories").json()["data"]
    for d in ("general", "reading", "fitness"):   # 条目领域 + 已建分类领域
        assert d in data["domains"]
    assert data["domains"] == sorted(data["domains"])


def test_panel_reanalyze_enqueues_job(api_client):
    client, app = api_client
    item_id = app.state.memory.add_item("general", "page", "待分析",
                                        content_text="内容")
    r = client.post(f"/items/{item_id}/reanalyze", follow_redirects=False)
    assert r.status_code == 303
    row = app.state.memory.db._conn().execute(
        "SELECT type, status FROM jobs WHERE type='auto_process'"
    ).fetchone()
    assert row["status"] == "pending"

    # 详情页迁到 Vue:按钮在组件里,数据与动作走接口
    page = client.get(f"/items/{item_id}").text
    assert 'id="item-app"' in page and "/static/dist/item.js" in page
    r2 = client.post(f"/api/items/{item_id}/reanalyze")
    assert r2.json()["data"]["queued"] == "auto_process"


def test_route_proposes_new_category_on_medium_confidence(memory, pstore, cfg):
    """提议是待确认动作,0.5~0.8 的中等置信度也应提议(而非默默留箱)。"""
    item_id = memory.add_item("general", "video", "技术评测",
                              content_text="对比各种 AI 编程订阅套餐")
    llm = StubLLM([{"category_id": None, "new_category": {"name": "技术笔记"},
                    "confidence": 0.65, "reason": "属于技术评测"}])
    result = route_item(memory, llm, pstore, item_id, cfg)
    assert result["action"] == "proposed"
    assert memory.db.get_category(result["category_id"])["status"] == "proposed"


def test_proposal_fallback_from_reason(memory, pstore, cfg):
    """LLM 把提议写进 reason 而漏掉结构化字段时的兜底解析。"""
    from memvault.classify import _proposal_from_reason

    assert _proposal_from_reason("内容属于技术评测,归入技术笔记(待确认)较合适。") \
        == "技术笔记"
    assert _proposal_from_reason("无合适分类,建议新增「穿搭灵感」分类") \
        == "穿搭灵感"
    assert _proposal_from_reason("内容为游戏解说") is None

    item_id = memory.add_item("general", "video", "技术评测",
                              content_text="AI 编程套餐对比")
    llm = StubLLM([{"category_id": None, "new_category": None,
                    "confidence": 0.6,
                    "reason": "属于技术评测,归入技术笔记(待确认)较合适"}])
    result = route_item(memory, llm, pstore, item_id, cfg)
    assert result["action"] == "proposed"
    assert memory.db.get_category(result["category_id"])["name"] == "技术笔记"


def test_extract_is_idempotent_no_key_accumulation(memory, pstore):
    """重新分析整体替换 attrs_ai,不同轮次的同义键不会累积(修复 #154 的15键问题)。"""
    cat_id = memory.db.add_category("cooking", "家常菜")
    item_id = memory.add_item("cooking", "video", "红烧肉",
                              content_text="两勺生抽小火炖", category_id=cat_id)
    import json

    llm1 = StubLLM([{"核心结论": "A", "成本数据": "10元"}])
    extract_item(memory, llm1, pstore, item_id)
    assert len(json.loads(memory.get_item(item_id)["attrs_ai"])) == 2

    llm2 = StubLLM([{"核心结论": "B", "关键要点": "C"}])  # 换了一组键名
    extract_item(memory, llm2, pstore, item_id)
    attrs = json.loads(memory.get_item(item_id)["attrs_ai"])
    assert set(attrs.keys()) == {"核心结论", "关键要点"}  # 旧键被整体替换
    assert attrs["核心结论"] == "B"


def test_extract_keeps_product_raw_attrs(memory, pstore):
    """商品采集时预写的价格/店铺在 attrs_json 中不受 AI 提取影响。"""
    item_id = memory.add_item("shopping", "product", "外套",
                              attrs={"price": "199元", "shop": "京东"})
    llm = StubLLM([{"颜色": "藏青"}])
    extract_item(memory, llm, pstore, item_id)
    import json

    item = memory.get_item(item_id)
    assert json.loads(item["attrs_json"])["price"] == "199元"
    assert json.loads(item["attrs_ai"])["颜色"] == "藏青"


def test_proposal_reuses_existing_same_name_category(memory, pstore, cfg):
    """提议同名分类时复用已有分类,不再创建重复(修复分类树出现两个'技术笔记')。"""
    cat_id = memory.db.add_category("general", "技术笔记")  # active
    item1 = memory.add_item("general", "video", "评测1", content_text="x")
    llm = StubLLM([{"category_id": None, "new_category": {"name": "技术笔记"},
                    "confidence": 0.9, "reason": "归类"}])
    r = route_item(memory, llm, pstore, item1, cfg)
    assert r["action"] == "filed" and r["category_id"] == cat_id
    assert len([c for c in memory.db.categories("general")
                if c["name"] == "技术笔记"]) == 1

    item2 = memory.add_item("general", "video", "评测2", content_text="y")
    llm2 = StubLLM([{"category_id": None, "new_category": {"name": "技术笔记"},
                     "confidence": 0.6, "reason": "归类"}])
    r2 = route_item(memory, llm2, pstore, item2, cfg)
    assert r2["action"] == "proposed" and r2["category_id"] == cat_id
    assert len([c for c in memory.db.categories("general")
                if c["name"] == "技术笔记"]) == 1


def test_proposal_strips_filler_words(memory, pstore, cfg):
    """理由文本里的语气词要剥离,避免'心理学书籍最合适'式重复分类。"""
    from memvault.classify import _proposal_from_reason

    assert _proposal_from_reason("归入心理学书籍最合适") == "心理学书籍"
    assert _proposal_from_reason("归类为技术笔记比较合适") == "技术笔记"
    assert _proposal_from_reason("建议新增「穿搭灵感」为宜") == "穿搭灵感"

    memory.db.add_category("reading", "心理学书籍")  # 已有 active
    item_id = memory.add_item("reading", "doc", "《影响力》",
                              content_text="心理学经典")
    llm = StubLLM([{"category_id": None, "new_category": None, "confidence": 0.6,
                    "reason": "属于心理学拆解,归入心理学书籍最合适"}])
    r = route_item(memory, llm, pstore, item_id, cfg)
    assert r["action"] == "proposed"
    cats = [c for c in memory.db.categories("reading")
            if c["name"] == "心理学书籍"]
    assert len(cats) == 1  # 复用了已有分类,没有新建重复


def test_proposal_fallback_on_belongs_to_phrase(memory, pstore, cfg):
    """理由只说"属于X类"时也要解析:真实 #13 因此漏解析而没归入已有分类。"""
    from memvault.classify import _proposal_hint

    assert _proposal_hint(
        "内容是对英剧《是,大臣》的剧情与政治讽刺进行解读,属于影视解读类。"
    ) == ("影视解读", False)
    # 显式措辞优先于"属于"("归入技术笔记" 胜过 "属于技术评测")
    assert _proposal_hint("内容属于技术评测,归入技术笔记较合适")[0] == "技术笔记"
    # 纯粹描述句里捞不出名字
    assert _proposal_hint("内容为游戏解说") == (None, False)

    cat_id = memory.db.add_category("general", "影视解读", status="proposed")
    item_id = memory.add_item("general", "video", "《是,大臣》解读",
                              content_text="政治讽刺剧评")
    llm = StubLLM([{"category_id": None, "new_category": None, "confidence": 0.98,
                    "reason": "内容是对英剧《是,大臣》的剧情解读,属于影视解读类。"}])
    r = route_item(memory, llm, pstore, item_id, cfg)
    assert r["action"] == "proposed"
    assert r["category_id"] == cat_id  # 命中已有待确认分类,而非留箱
    item = memory.get_item(item_id)
    assert item["category_id"] == cat_id
    assert item["status"] == "inbox"  # 确认前不转 filed


def test_explicit_proposal_ignores_confidence_gate(memory, pstore, cfg):
    """理由明确"需新建X分类"时低置信度也提议:真实 #14(conf 0.3)被阈值丢弃。

    提议是待用户确认的动作,显式点名时不必再看置信度;弱措辞仍守 0.5。
    """
    item_id = memory.add_item("general", "video", "12个冰淇淋指南",
                              content_text="实为二手平台带货推广")
    llm = StubLLM([{"category_id": None, "new_category": None, "confidence": 0.3,
                    "reason": "内容实为转转二手平板的促销推广,与树中分类均不匹配,"
                              "需新建数码消费类分类。"}])
    r = route_item(memory, llm, pstore, item_id, cfg)
    assert r["action"] == "proposed"
    assert memory.db.get_category(r["category_id"])["name"] == "数码消费"

    # 弱措辞 + 低置信度:仍留箱,不造垃圾提议
    item2 = memory.add_item("general", "video", "随拍", content_text="不明内容")
    llm2 = StubLLM([{"category_id": None, "new_category": None, "confidence": 0.3,
                     "reason": "内容属于生活记录"}])
    r2 = route_item(memory, llm2, pstore, item2, cfg)
    assert r2["action"] == "inbox"
    assert len([c for c in memory.db.categories("general")
                if c["name"] == "生活记录"]) == 0


def test_proposal_reuses_variant_name_category(memory, pstore, cfg):
    """词尾变体视为同一桶:真实 #13 造出了「影视解读范畴」与「影视解读」并存。"""
    from memvault.classify import _proposal_hint, same_category_name

    assert _proposal_hint("属于影视解读范畴") == ("影视解读", False)
    assert _proposal_hint("建议新增「技术笔记」类目")[0] == "技术笔记"
    assert same_category_name("影视解读", "影视解读范畴")
    assert same_category_name("影视解读范畴", "影视解读")
    assert not same_category_name("影视解读", "影视旁白")   # 词尾不是泛化词
    assert not same_category_name("读书", "读书笔记")

    cat_id = memory.db.add_category("general", "影视解读", status="proposed")
    item_id = memory.add_item("general", "video", "《是,大臣》解读",
                              content_text="政治讽刺剧评")
    llm = StubLLM([{"category_id": None, "new_category": None, "confidence": 0.96,
                    "reason": "对英剧的剧情与创作背景解读,属于影视解读范畴。"}])
    r = route_item(memory, llm, pstore, item_id, cfg)
    assert r["action"] == "proposed" and r["category_id"] == cat_id
    assert len([c for c in memory.db.categories("general")
                if "影视解读" in c["name"]]) == 1  # 没有造出重复分类


def test_extraction_text_covers_long_content():
    """长视频正文要覆盖全文,而不是只看前 3000 字(真实 #11 只覆盖 18%)。"""
    from memvault.classify import _extraction_text

    assert _extraction_text({"content_text": "短内容"}) == "短内容"

    long_text = ("开头交代主题。" + "".join(f"第{i}段讲述细节。" for i in range(1500))
                 + "最后给出结论总结。")
    out = _extraction_text({"content_text": long_text}, budget=3000)
    assert len(out) <= 3000                   # 总量受控(省略标记也计入预算)
    assert out.startswith("开头交代主题。")     # 开头保留
    assert "最后给出结论总结。" in out          # 结尾整段保留(结论所在)
    assert "……(省略)……" in out                # 中段是抽样,不是截断

    # 无 content_text 时退化为拼接语义块,同样受预算约束
    item = {"title": "T", "chunks": [
        {"modality": "text", "content": "块一" * 800},
        {"modality": "image", "content": None},
        {"modality": "text", "content": "块二结尾"}]}
    out2 = _extraction_text(item, budget=500)
    assert len(out2) <= 520 and "块二结尾" in out2


def test_auto_process_skips_deleted_item(memory, cfg):
    """同源重跑删掉的条目,其排队任务静默跳过而非失败(真实 job#134)。"""
    class ExplodingLLM:
        enabled = True

        def chat_json(self, system, user, **kw):
            raise AssertionError("条目不存在时不应调用 LLM")

    result = auto_process({"item_id": 999}, memory, cfg, llm=ExplodingLLM())
    assert result["action"] == "gone"


# ── UP主 → 分类 规则(q3:把一个 UP 绑到分类,他的视频自动归档)────────
def test_up_rule_files_item_without_llm(memory, pstore, cfg):
    """绑了 UP 规则就直接归档,不再调用 LLM(省一次分类调用)。"""
    from memvault.classify import route_item

    cat_id = memory.db.add_category("general", "冰淇淋教程")
    memory.db.bind_up_category("12345", "胡仔一人食", cat_id)
    item_id = memory.db.add_item(
        "general", "video", "某视频", status="inbox",
        attrs={"up": "胡仔一人食", "up_mid": "12345"})

    class ExplodingLLM:
        enabled = True

        def chat_json(self, *a, **kw):
            raise AssertionError("命中 UP 规则时不应调用 LLM")

    r = route_item(memory, ExplodingLLM(), pstore, item_id, cfg)
    assert r["action"] == "filed" and r["category_id"] == cat_id
    assert r.get("by") == "up_rule"
    item = memory.get_item(item_id)
    assert item["status"] == "filed" and item["category_conf"] == 1.0
    assert "UP主规则" in item["auto_note"]


def test_up_rule_missing_or_unbound_falls_back(memory, pstore, cfg):
    """没有 UP 信息 / 没绑规则 → 照常走 LLM 路由。"""
    from memvault.classify import route_item

    cat_id = memory.db.add_category("general", "技术")
    # 有 up_mid 但没绑定
    a = memory.db.add_item("general", "video", "技术视频",
                           attrs={"up": "某UP", "up_mid": "999"})
    llm = StubLLM([{"category_id": cat_id, "new_category": None,
                    "confidence": 0.9, "reason": "技术类"}])
    assert route_item(memory, llm, pstore, a, cfg)["action"] == "filed"

    # 完全没有 UP 信息
    b = memory.db.add_item("general", "video", "另一个视频")
    llm2 = StubLLM([{"category_id": cat_id, "new_category": None,
                     "confidence": 0.9, "reason": "技术类"}])
    assert route_item(memory, llm2, pstore, b, cfg)["category_id"] == cat_id


def test_up_rule_crud(memory):
    """绑定/查询/解绑/列表。"""
    cat = memory.db.add_category("general", "音乐教程")
    memory.db.bind_up_category("888", "某唱见", cat)
    rule = memory.db.up_category("888")
    assert rule["category_id"] == cat and rule["up_name"] == "某唱见"

    # 重复绑定视为更新(换分类)
    cat2 = memory.db.add_category("general", "唱歌教学")
    memory.db.bind_up_category("888", "某唱见", cat2)
    assert memory.db.up_category("888")["category_id"] == cat2
    assert len(memory.db.up_rules()) == 1

    assert memory.db.up_category("000") is None      # 未绑定
    assert memory.db.unbind_up_category("888") == 1
    assert memory.db.up_rules() == []


def test_panel_bind_up_and_list(api_client):
    """面板:条目页绑定 UP → 分类页能看到规则,可解除。"""
    client, app = api_client
    cat = app.state.memory.db.add_category("general", "冰淇淋教程")
    item = app.state.memory.add_item("general", "video", "冰淇淋视频",
                                     attrs={"up": "胡仔一人食", "up_mid": "777"})

    # 详情页迁到 Vue:UP 信息与绑定动作走接口
    d = client.get(f"/api/items/{item}").json()["data"]
    assert d["up"]["up_mid"] == "777" and d["up"]["up_name"] == "胡仔一人食"

    r = client.post(f"/api/items/{item}/bind-up",
                    json={"category_id": cat, "up_mid": "777",
                          "up_name": "胡仔一人食"})
    assert r.json()["ok"] is True and r.json()["data"]["category_name"] == "冰淇淋教程"
    assert app.state.memory.db.up_category("777")["category_id"] == cat
    assert app.state.memory.get_item(item)["category_id"] == cat

    # 分类页迁到 Vue:规则由 /api/categories.up_rules 提供(组件渲染该区块)
    page = client.get("/categories").text
    assert 'id="categories-app"' in page
    rules = client.get("/api/categories").json()["data"]["up_rules"]
    assert rules and rules[0]["up_mid"] == "777"
    assert rules[0]["up_name"] == "胡仔一人食"
    assert rules[0]["category_name"] == "冰淇淋教程"

    # 解除绑定:接口与旧表单端点(双轨)都能解
    assert client.post("/api/up/777/unbind").json()["data"]["removed"] == 1
    assert app.state.memory.db.up_rules() == []
    app.state.memory.db.bind_up_category("777", "胡仔一人食", cat)
    r2 = client.post("/up/777/unbind", data={"back": "categories"},
                     follow_redirects=False)
    assert r2.status_code == 303
    assert app.state.memory.db.up_rules() == []


def test_delete_category_returns_items_to_inbox(memory):
    """删除分类:条目退回待整理箱,专属提示词/质疑与 UP 规则一并清理。"""
    cat = memory.db.add_category("general", "临时分类")
    keep = memory.db.add_category("general", "保留分类")
    pstore = PromptStore(memory.db)
    pstore.ensure_seed()
    pstore.new_version("extract", "extract", "该分类专属提示词", category_id=cat)

    item_filed = memory.db.add_item("general", "video", "归好的", status="filed",
                                    category_id=cat)
    item_inbox = memory.db.add_item("general", "video", "还在箱里",
                                    category_id=cat, status="inbox")
    other = memory.db.add_item("general", "video", "别人的", status="filed",
                               category_id=keep)
    memory.db.bind_up_category("555", "某UP", cat)

    info = memory.db.delete_category(cat)
    assert info["name"] == "临时分类" and info["items"] == 2
    assert info["prompts"] == 1 and info["up_rules"] == 1

    assert memory.db.get_category(cat) is None
    a = memory.get_item(item_filed)
    assert a["category_id"] is None and a["status"] == "inbox"   # filed → inbox
    b = memory.get_item(item_inbox)
    assert b["category_id"] is None and b["status"] == "inbox"
    assert memory.get_item(other)["category_id"] == keep          # 别的分类不动
    assert memory.db.up_category("555") is None
    n = memory.db._conn().execute(
        "SELECT COUNT(*) FROM prompts WHERE category_id=?", (cat,)).fetchone()[0]
    assert n == 0


def test_delete_category_missing_and_panel(api_client):
    """删除不存在的分类 → 404;面板删除后列表里消失。"""
    client, app = api_client
    assert app.state.memory.db.delete_category(9999) is None
    assert client.post("/categories/9999/delete",
                       follow_redirects=False).status_code == 404

    cat = app.state.memory.db.add_category("general", "要删掉的")
    names = [c["name"] for c in client.get("/api/categories").json()["data"]["items"]]
    assert "要删掉的" in names

    # 旧表单端点(双轨)与接口删除都应生效
    r = client.post(f"/categories/{cat}/delete", follow_redirects=False)
    assert r.status_code == 303
    names = [c["name"] for c in client.get("/api/categories").json()["data"]["items"]]
    assert "要删掉的" not in names
    cat2 = app.state.memory.db.add_category("general", "再删一次")
    assert client.post(f"/api/categories/{cat2}/delete").json()["data"]["name"] == "再删一次"
    assert app.state.memory.db.get_category(cat2) is None


def test_extract_ads_policy_in_prompt(memory, pstore, cfg):
    """广告策略:默认忽略;设置成 mention 时提示词里换成"单独一条属性"。"""
    from memvault.classify import ads_policy_text, extract_item

    cat_id = memory.db.add_category("general", "美食教程")
    item_id = memory.add_item("general", "video", "某视频",
                              content_text="演示:一段带广告的做法视频",
                              category_id=cat_id)
    captured = {}

    class RecLLM:
        enabled = True

        def chat_json(self, system, user, **kw):
            captured["system"] = system
            return {"主题": "演示"}

    pstore.ensure_seed()
    extract_item(memory, RecLLM(), pstore, item_id, cfg)          # cfg 里默认 ignore
    assert "广告与推广内容一律忽略" in captured["system"]
    assert "{ads_policy}" not in captured["system"]               # 占位符必须被替换
    assert "{category}" not in captured["system"]

    cfg2 = {**cfg, "llm": {**cfg["llm"], "ads_policy": "mention"}}
    assert "单独归入一条属性" in ads_policy_text(cfg2)
    extract_item(memory, RecLLM(), pstore, item_id, cfg2)
    assert "单独归入一条属性" in captured["system"]
    assert "一律忽略" not in captured["system"]


def test_factory_prompt_upgrades_but_user_rewrite_is_kept(memory):
    """出厂提示词跟随升级;被改写过的版本保留(符合"可进化 + 可回滚")。"""
    from memvault.prompts import EXTRACT_PROMPT, EXTRACT_PROMPT_V1, PromptStore

    pstore = PromptStore(memory.db)
    pstore.new_version("extract", "extract", EXTRACT_PROMPT_V1)   # 装作老库
    pstore.ensure_seed()
    active = pstore.get_active("extract", "extract")
    assert "并列对象" in active["content"] and active["origin"] == "upgrade"
    old = [v for v in pstore.versions("extract", "extract") if v["version"] == 1]
    assert old and old[0]["status"] == "retired"                  # 旧版可回滚

    # 用户改写过的提示词不被覆盖
    pstore.new_version("extract", "extract", "我自己改的提示词:只输出一句话")
    pstore.ensure_seed()
    assert "我自己改的" in pstore.get_active("extract", "extract")["content"]
