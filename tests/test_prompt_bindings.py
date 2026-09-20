"""购物稿 ①②:提示词规则绑定 + 关键片段(clips)。"""
import pytest


@pytest.fixture()
def pstore(memory):
    from memvault.prompts import PromptStore

    ps = PromptStore(memory.db)
    ps.ensure_seed()
    return ps


def test_prompt_binding_resolution_priority(memory):
    """规则命中顺序:UP → UP集 → 领域 → 来源;都没命中返回 None(走原逻辑)。"""
    from memvault.prompts import resolve_prompt_name

    memory.db.bind_prompt("up", "123", "shopping_review", note="带货号")
    memory.db.bind_prompt("domain", "shopping", "shopping_review")
    item_up = {"domain": "general", "source_type": "video", "title": "x",
               "attrs_json": '{"up_mid": "123"}'}
    assert resolve_prompt_name(memory, item_up) == "shopping_review"

    item_dom = {"domain": "shopping", "source_type": "webpage", "title": "y",
                "attrs_json": "{}"}
    assert resolve_prompt_name(memory, item_dom) == "shopping_review"

    item_none = {"domain": "reading", "source_type": "file", "title": "z",
                 "attrs_json": "{}"}
    assert resolve_prompt_name(memory, item_none) is None

    # UP集(逗号分隔)与解绑
    memory.db.bind_prompt("up_set", "1,2, 2345", "shopping_review")
    assert resolve_prompt_name(
        memory, {"domain": "general", "source_type": "video", "title": "t",
                 "attrs_json": '{"up_mid": "2345"}'}) == "shopping_review"
    assert memory.db.unbind_prompt("up", "123") == 1
    assert all(r["prompt_name"] != "" for r in memory.db.prompt_bindings())


def test_snap_clips_to_units(memory):
    """片段边界吸附到单元边界;非法片段丢弃(设计稿 §3 约定 4)。"""
    from memvault.links import snap_clips_to_units

    item = memory.add_item("general", "video", "购物视频")
    for i, (s, e) in enumerate([(0.0, 5.0), (5.0, 12.0), (12.0, 20.0)]):
        memory.add_text_chunk(item, f"[单元 {s:.0f}-{e:.0f}]\n字幕:测试", start_ts=s,
                              end_ts=e, seq=100 + i)
    raw = [
        {"start": 0.4, "end": 4.6, "kind": "卖点演示", "confidence": 0.8},
        {"start": 6.0, "end": 11.5, "kind": "价格播报"},
        {"start": -3.0, "end": 2.0, "kind": "非法负值"},
        {"start": 5.0, "end": 5.1, "kind": "太短"},
        {"start": 3.0, "end": 9.0, "kind": "跨单元"},
    ]
    out = snap_clips_to_units(memory, item, raw)
    # 0.4→0.0/4.6→5.0;6.0→5.0/11.5→12.0;跨单元的 3.0~9.0 → 0.0~12.0(吸附到两侧单元边界)
    assert [c["start_ts"] for c in out] == [0.0, 0.0, 5.0]
    ends = sorted(c["end_ts"] for c in out)
    assert ends == [5.0, 12.0, 12.0]
    assert all(c["kind"] not in ("非法负值", "太短") for c in out)
    # 没有单元块时原样返回(未单元化视频)
    plain = memory.add_item("general", "video", "普通视频")
    out2 = snap_clips_to_units(memory, plain,
                               [{"start": 1.0, "end": 4.0, "kind": "x"}])
    assert out2[0]["start_ts"] == 1.0 and out2[0]["end_ts"] == 4.0


def test_extract_writes_clips_when_profile_hits(memory, pstore):
    """命中规则 → 用购物复盘提示词 → 「关键片段」落成 clips(幂等替换)。"""

    class StubLLM:
        enabled = True

        def __init__(self, resp):
            self.resp = resp

        def chat_json(self, system, user, **kw):
            assert "购物向视频复盘" in system        # 用的是 profile 提示词
            return self.resp

    item = memory.add_item("general", "video", "带货视频",
                           attrs={"up": "某UP", "up_mid": "999"})
    memory.add_text_chunk(item, "[单元 00:00-00:10]\n字幕:今天这件外套", start_ts=0.0,
                          end_ts=10.0, seq=100)
    memory.db.bind_prompt("up", "999", "shopping_review")

    resp = {"商品": [{"名称": "冲锋衣", "价格": "899"}],
            "卖点": [{"点": "抗风", "证据": "原话", "ts": 3.0, "来源": "asr"}],
            "关键片段": [{"start": 0.5, "end": 8.0, "kind": "卖点演示",
                          "reason": "面料演示", "confidence": 0.9}]}
    from memvault.classify import extract_item

    out = extract_item(memory, StubLLM(resp), pstore, item)
    assert out["profile"] == "shopping_review" and out["clips"] == 1
    clips = memory.db.clips_for_item(item)
    assert clips[0]["kind"] == "卖点演示" and clips[0]["start_ts"] == 0.0
    assert clips[0]["end_ts"] == 10.0             # 吸附到单元边界
    import json
    assert json.loads(memory.get_item(item)["attrs_ai"])["卖点"][0]["点"] == "抗风"

    # 再跑一次不累积(整体替换)
    extract_item(memory, StubLLM(resp), pstore, item)
    assert len(memory.db.clips_for_item(item)) == 1


def test_item_api_exposes_clips(memory, tmp_path):
    """/api/items/{id} 返回 clips(含时间标签与跳转链接)。"""
    from fastapi.testclient import TestClient

    from memvault.config import load_config
    from memvault.server.app import create_app

    cfg = load_config()
    cfg["data_dir"] = str(tmp_path / "data")
    cfg["embedding"]["fake"] = True
    cfg["llm"] = dict(cfg["llm"], api_key=None)
    app = create_app(cfg)
    with TestClient(app) as client:
        m = app.state.memory
        item = m.add_item("general", "video", "带货视频",
                          source_ref="BV1TEST")
        m.db.set_clips(item, [{"start_ts": 12.0, "end_ts": 20.0,
                               "kind": "价格播报", "reason": "报价格",
                               "confidence": 0.7}])
        d = client.get(f"/api/items/{item}").json()["data"]
        assert d["clips"] and d["clips"][0]["kind"] == "价格播报"
        assert d["clips"][0]["start_label"] == "00:12"
        assert d["clips"][0]["jump"].endswith("BV1TEST?t=12")


def test_prompt_bindings_table_exists_in_schema():
    """表在 SCHEMA 里(老库启动自动建表,不需要手工迁移)。"""
    from memvault.db import SCHEMA

    assert "prompt_bindings" in SCHEMA and "CREATE TABLE IF NOT EXISTS clips" in SCHEMA
    assert "stage" in SCHEMA           # frames/extract/clip 共用一张表(红线③)
