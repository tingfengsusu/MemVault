"""M4:技能化检索与加购执行。LLM 注入 Stub,京东注入 FakeHandler。"""
import pytest

from memvault.chat import chat_turn
from memvault.skills import get_skill


class StubLLM:
    enabled = True

    def __init__(self, json_responses, text_reply="好的。"):
        self.json_responses = list(json_responses)
        self.text_reply = text_reply
        self.calls = []

    def chat_json(self, system, user, **kw):
        self.calls.append((system, user))
        return self.json_responses.pop(0)

    def chat(self, system, user, **kw):
        self.calls.append((system, user))
        return self.text_reply


def test_skill_registry():
    assert get_skill("fitness").timeline_domain == "fitness"
    assert get_skill("fitness").search_kwargs == {"domain": "fitness"}
    assert get_skill("shopping").search_kwargs == {"domain": "shopping"}
    assert get_skill("unknown").key == "general"  # 未知技能回落通用
    assert get_skill(None).key == "general"


def test_fitness_skill_filters_domain(memory, cfg):
    fit = memory.add_item("fitness", "video", "深蹲教学视频",
                          content_text="深蹲动作讲解")
    memory.add_text_chunk(fit, "深蹲动作讲解", start_ts=10)
    cook = memory.add_item("cooking", "video", "厨房技巧",
                           content_text="这个深蹲动作讲解是烹饪术语干扰项")
    memory.add_text_chunk(cook, "这个深蹲动作讲解是烹饪术语干扰项")

    llm = StubLLM(
        [{"profile": [], "log": None, "search_query": "深蹲动作讲解"}],
        text_reply="注意膝盖。")
    chat_turn(memory, llm, "深蹲有什么要注意的?", skill="fitness")

    _, user = llm.calls[-1]  # 回复调用的 user 含上下文
    assert "深蹲教学视频" in user          # fitness 条目进入上下文
    assert "厨房技巧" not in user          # 其他领域被技能过滤
    assert "教练" in llm.calls[-1][0]      # 使用教练提示词


def test_general_skill_not_filtered(memory, cfg):
    cook = memory.add_item("cooking", "video", "红烧肉",
                           content_text="起锅烧油放入冰糖炒糖色")
    memory.add_text_chunk(cook, "起锅烧油放入冰糖炒糖色")
    llm = StubLLM([{"profile": [], "log": None,
                    "search_query": "起锅烧油放入冰糖炒糖色"}])
    chat_turn(memory, llm, "红烧肉怎么炒糖色?", skill="general")
    _, user = llm.calls[-1]
    assert "红烧肉" in user


def test_jd_cart_job_success(memory, cfg, monkeypatch):
    from memvault.worker import build_dispatch, step

    calls = []

    class FakeHandler:
        def login(self): calls.append("login")

        def search_and_add(self, kw):
            calls.append(f"add:{kw}")
            return True

        def close(self): calls.append("close")

    monkeypatch.setattr("memvault.automation.jd.JdHandler", FakeHandler)
    memory.db.enqueue("jd_cart", {"keyword": "优衣库摇粒绒外套"})
    dispatch = build_dispatch(memory, cfg)
    step(memory, cfg, dispatch=dispatch)

    assert calls == ["login", "add:优衣库摇粒绒外套", "close"]
    row = memory.db._conn().execute(
        "SELECT status FROM jobs WHERE type='jd_cart'").fetchone()
    assert row["status"] == "done"


def test_jd_cart_job_failure_marks_failed(memory, cfg, monkeypatch):
    from memvault.worker import build_dispatch, step

    class FakeHandler:
        def login(self): pass

        def search_and_add(self, kw): return False

        def close(self): pass

    monkeypatch.setattr("memvault.automation.jd.JdHandler", FakeHandler)
    memory.db.enqueue("jd_cart", {"keyword": "不存在的商品xyz"})
    step(memory, cfg, dispatch=build_dispatch(memory, cfg))
    row = memory.db._conn().execute(
        "SELECT status, error FROM jobs WHERE type='jd_cart'").fetchone()
    assert row["status"] == "failed"
    assert "京东加购失败" in row["error"]


def test_item_cart_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from memvault import llm as llm_mod
    from memvault.config import load_config
    from memvault.server.app import create_app

    monkeypatch.setattr(llm_mod, "_VIDEO2SHOP_CONFIG", tmp_path / "no.yaml")
    monkeypatch.setattr(llm_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = load_config()
    cfg["data_dir"] = str(tmp_path)
    cfg["embedding"]["fake"] = True
    cfg["llm"] = dict(cfg["llm"], api_key=None)
    app = create_app(cfg)
    with TestClient(app) as client:
        item_id = app.state.memory.add_item(
            "shopping", "product", "优衣库摇粒绒外套",
            attrs={"price": "199元"}, source_type="webpage")
        r = client.post(f"/items/{item_id}/cart", follow_redirects=False)
        assert r.status_code == 303
        row = app.state.memory.db._conn().execute(
            "SELECT type, payload FROM jobs WHERE type='jd_cart'").fetchone()
        assert row and "优衣库摇粒绒外套" in row["payload"]

        assert client.post("/items/99999/cart",
                           follow_redirects=False).status_code == 404

        # 详情页迁到 Vue 后走 JSON 端点;页面只保留外壳
        r2 = client.post(f"/api/items/{item_id}/cart")
        assert r2.json()["ok"] is True
        n = app.state.memory.db._conn().execute(
            "SELECT COUNT(*) FROM jobs WHERE type='jd_cart'").fetchone()[0]
        assert n == 2                      # 旧端点 + 新端点各入队一次
        assert client.post("/api/items/99999/cart").status_code == 404
        page = client.get(f"/items/{item_id}").text
        assert 'id="item-app"' in page
