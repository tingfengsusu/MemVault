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

    attrs = json.loads(memory.get_item(item_id)["attrs_json"])
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
    client, app = api_client
    r = client.post("/categories/add",
                    data={"domain": "fitness", "name": "胸部"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/categories")
    assert "胸部" in page.text

    # 模拟 LLM 提议的分类 → 确认采纳
    cat_id = app.state.memory.db.add_category("fitness", "腿部",
                                              status="proposed")
    page = client.get("/categories")
    assert "待确认" in page.text
    r = client.post(f"/categories/{cat_id}/confirm", follow_redirects=False)
    assert r.status_code == 303
    assert app.state.memory.db.get_category(cat_id)["status"] == "active"


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

    html = client.get(f"/items/{item_id}").text
    assert "重新分析" in html
