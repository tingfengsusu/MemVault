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


def test_extract_uses_profile_and_validates_schema(memory, pstore):
    """命中规则 → 用购物复盘提示词;返回不符合 schema 时**不写 attrs**(实测踩过)。"""

    class StubLLM:
        enabled = True

        def __init__(self, resp):
            self.resp = resp

        def chat_json(self, system, user, **kw):
            assert "购物向视频复盘" in system        # 用的是 profile 提示词
            return self.resp

    item = memory.add_item("general", "video", "带货视频",
                           attrs={"up": "某UP", "up_mid": "999"})
    memory.add_text_chunk(item, "[单元 00:00-00:10]\n字幕:今天这件外套",
                          start_ts=0.0, end_ts=10.0, seq=100)
    memory.db.bind_prompt("up", "999", "shopping_review")

    resp = {"商品": [{"名称": "冲锋衣", "价格": "899"}],
            "卖点": [{"点": "抗风", "证据": "原话", "ts": 3.0, "来源": "asr"}]}
    from memvault.classify import extract_item

    out = extract_item(memory, StubLLM(resp), pstore, item)
    assert out["profile"] == "shopping_review"
    import json
    assert json.loads(memory.get_item(item)["attrs_ai"])["卖点"][0]["点"] == "抗风"

    # 模型跑偏(返回无关结构)→ schema 校验拦住,不覆盖已有 attrs
    out2 = extract_item(memory, StubLLM({"type": "text", "content": "…"}), pstore, item)
    assert out2.get("error") == "schema_mismatch"
    assert json.loads(memory.get_item(item)["attrs_ai"])["卖点"][0]["点"] == "抗风"


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


# ── 第 4 步:UP 画像缓存与校验 ────────────────────────────────────────
def _prof(bands=((0.5, 0.542),), events=80, duration=400.0, speech=0.08):
    from memvault.vision.frames_probe import VideoProfile

    p = VideoProfile(ok=True, duration=duration, events=[(i * 5.0, i * 5.0 + 3) for i in range(events)],
                     subtitle_bands=list(bands), speech_ratio=speech)
    return p


def test_frames_profile_cache_roundtrip(memory):
    """画像缓存在 prompt_bindings(stage='frames'),不另建规则表。"""
    from memvault.vision.frames_probe import profile_payload

    assert memory.db.frames_profile("777") is None
    memory.db.save_frames_profile("777", profile_payload(_prof()))
    got = memory.db.frames_profile("777")
    assert got["subtitle_bands"][0] == [0.5, 0.542]
    assert got["event_hz"] and got["speech_ratio"] == 0.08
    n = memory.db.unbind_prompt("up", "777", stage="frames")
    assert n == 1 and memory.db.frames_profile("777") is None


def test_validate_against_cached_profile():
    """偏离字幕带/事件频率/人声占比 → 判"不符合该 UP 常规形态"。"""
    from memvault.vision.frames_probe import profile_payload, validate_against

    cached = profile_payload(_prof())
    ok, why = validate_against(_prof(), cached)
    assert ok and "符合" in why
    # 字幕带位置大幅偏移(>5% 画面高)
    ok2, why2 = validate_against(_prof(bands=((0.72, 0.76),)), cached)
    assert not ok2 and "偏移" in why2
    # 人声占比跳到另一档(旁白视频)
    ok3, why3 = validate_against(_prof(speech=0.75), cached)
    assert not ok3 and "人声" in why3
    # 没判出字幕带
    ok4, why4 = validate_against(_prof(bands=()), cached)
    assert not ok4 and "没判出" in why4


# ── 第 5 步:弹幕广告段 ──────────────────────────────────────────────
def test_detect_ad_segments_votes_by_window():
    """同一时间窗内 ≥2 条广告词才算广告段;零散玩笑话被滤掉(实测 339/349s)。"""
    from memvault.sources.bili_danmaku import detect_ad_segments

    danmaku = [
        {"ts": 175.0, "text": "本期由旺仔牛奶赞助"},
        {"ts": 176.0, "text": "谢谢金主妈妈"},
        {"ts": 180.0, "text": "感谢金主爸爸"},
        {"ts": 178.0, "text": "金主妈妈好"},
        {"ts": 184.0, "text": "谢谢金主妈妈"},
        {"ts": 339.0, "text": "金主又来代言了"},   # 零散玩笑(单条)
        {"ts": 349.0, "text": "甲方真会挑人"},     # 零散玩笑(隔了 10s)
        {"ts": 100.0, "text": "这个配方不错"},     # 无关
    ]
    segs = detect_ad_segments(danmaku, window=10, min_hits=2)
    assert segs == [(170.0, 190.0)]
    # 提高门槛到 3 条 → 只剩最密集的那个窗(170~180s)
    assert detect_ad_segments(danmaku, window=10, min_hits=3) == [(170.0, 180.0)]
    assert detect_ad_segments([], window=10, min_hits=2) == []
    assert detect_ad_segments([{"ts": 1.0, "text": "普通"}], ) == []


def test_danmaku_bare_deflate_decode():
    """裸 deflate(finish=length 之外的坑):注释里记的是 Content-Encoding: deflate。"""
    import zlib

    xml = ('<?xml version="1.0"?><i><d p="175.0,1,25,16777215">本期由旺仔牛奶赞助</d></xml>')
    bare = zlib.compress(xml.encode("utf-8"))[2:-4]      # 去掉 zlib 头尾 = 裸 deflate
    import re

    from memvault.sources.bili_danmaku import _AD_RE

    text = zlib.decompress(bare, -15).decode("utf-8")
    assert _AD_RE.search(text)


def test_clips_snap_switch_config():
    """吸附开关(clips.snap=off 回退到 LLM 原始边界)。"""
    from memvault.config import DEFAULTS

    assert DEFAULTS["clips"]["snap"] in ("on", "off")


# ── 管理面:规则绑定 API ─────────────────────────────────────────────
def _client(tmp_path):
    from fastapi.testclient import TestClient

    from memvault.config import load_config
    from memvault.server.app import create_app

    cfg = load_config()
    cfg["data_dir"] = str(tmp_path / "data")
    cfg["embedding"]["fake"] = True
    cfg["llm"] = dict(cfg["llm"], api_key=None)
    return TestClient(create_app(cfg)), cfg


def test_prompt_binding_api_crud(tmp_path):
    """设置页用的规则管理接口:列表 / 绑定 / 解绑 / 清画像缓存。"""
    client, cfg = _client(tmp_path)
    with client:
        d = client.get("/api/prompt-bindings").json()["data"]
        assert "shopping_review" in d["profiles"]          # 种子 profile 已就绪
        assert d["kinds"][0] == "up" and d["items"] == []

        r = client.post("/api/prompt-bindings", json={
            "kind": "up", "target": "12345",
            "prompt_name": "shopping_review", "note": "带货号"})
        assert r.status_code == 200 and r.json()["ok"]
        d2 = client.get("/api/prompt-bindings").json()["data"]
        assert d2["items"][0]["target"] == "12345"
        assert d2["items"][0]["note"] == "带货号"

        # 非法 kind / 不存在的 profile
        assert client.post("/api/prompt-bindings",
                           json={"kind": "wat", "target": "1",
                                 "prompt_name": "shopping_review"}).status_code == 422
        bad = client.post("/api/prompt-bindings",
                          json={"kind": "up", "target": "1",
                                "prompt_name": "no_such_profile"})
        assert bad.status_code == 404
        assert bad.json()["error"]["code"] == "not_found"

        # 解绑
        assert client.post("/api/prompt-bindings/unbind", json={
            "kind": "up", "target": "12345"}).json()["data"]["removed"] == 1
        assert client.get("/api/prompt-bindings").json()["data"]["items"] == []


def test_frames_profile_cache_api(tmp_path):
    """UP 画像缓存:采集后写入,管理面可见并可清除(第 4 步的可见性)。"""
    client, cfg = _client(tmp_path)
    with client:
        m = client.app.state.memory if hasattr(client, "app") else None
    # 直接操作 app.state.memory 更稳
    from fastapi.testclient import TestClient

    client2, cfg2 = _client(tmp_path / "b")
    with client2:
        app_mem = client2.app.state.memory
        app_mem.db.save_frames_profile("777", {"subtitle_bands": [[0.5, 0.542]],
                                               "event_hz": 0.2, "speech_ratio": 0.08,
                                               "duration": 391.0})
        d = client2.get("/api/prompt-bindings").json()["data"]
        assert d["caches"] and d["caches"][0]["up_mid"] == "777"
        assert d["caches"][0]["speech_ratio"] == 0.08
        assert client2.post("/api/frames-profiles/clear",
                            json={"up_mid": "777"}).json()["data"]["removed"] == 1
        assert client2.get("/api/prompt-bindings").json()["data"]["caches"] == []


# ── 关键片段:独立小 schema 调用(单元编号引用)──────────────────────
def _units(memory, item, spans):
    for i, (s, e) in enumerate(spans):
        memory.add_text_chunk(item, f"[单元 {s:.0f}-{e:.0f}]\n字幕:单元{i+1}内容",
                              start_ts=s, end_ts=e, seq=100 + i)


def test_extract_clips_maps_unit_ids(memory, pstore):
    """模型只引用单元编号 → 时间戳取单元边界;引用不存在的编号被丢弃。"""
    from memvault.classify import extract_clips

    class Stub:
        enabled = True

        def chat_json(self, system, user, **kw):
            assert "只引用单元编号" in system          # 用的是小 schema 提示词
            assert "#1" in user and "#3" in user       # 单元列表带编号
            return {"clips": [
                {"from_unit": 1, "to_unit": 2, "kind": "卖点演示",
                 "reason": "面料演示", "confidence": 0.9},
                {"from_unit": 3, "to_unit": 3, "kind": "价格播报"},
                {"from_unit": 9, "to_unit": 9, "kind": "越界"},   # 不存在 → 丢
                {"from_unit": 2, "to_unit": 1, "kind": "倒序"},   # 倒序 → 自动摆正
            ]}

    item = memory.add_item("general", "video", "带货视频")
    _units(memory, item, [(0.0, 10.0), (10.0, 22.0), (22.0, 30.0)])
    clips = extract_clips(memory, Stub(), pstore, item)
    kinds = sorted(c["kind"] for c in clips)
    assert "越界" not in kinds
    # 倒序那条(2→1)与"卖点演示"同区间 → 去重,只剩 2 段
    assert len(clips) == 2
    by_kind = {c["kind"]: c for c in clips}
    assert by_kind["卖点演示"]["start_ts"] == 0.0 and by_kind["卖点演示"]["end_ts"] == 22.0
    assert by_kind["价格播报"]["start_ts"] == 22.0 and by_kind["价格播报"]["end_ts"] == 30.0
    # 落库且幂等
    assert len(memory.db.clips_for_item(item)) == 2
    extract_clips(memory, Stub(), pstore, item)
    assert len(memory.db.clips_for_item(item)) == 2


def test_extract_clips_handles_bad_shapes(memory, pstore):
    """返回结构不符/无单元/异常时都安静返回空(不影响主流程)。"""
    from memvault.classify import extract_clips

    class Bad:
        enabled = True

        def chat_json(self, system, user, **kw):
            return {"type": "text", "content": "..."}     # 模型跑偏的返回

    class Boom:
        enabled = True

        def chat_json(self, system, user, **kw):
            raise RuntimeError("budget")

    no_units = memory.add_item("general", "video", "没有单元的")
    assert extract_clips(memory, Bad(), pstore, no_units) == []

    item = memory.add_item("general", "video", "有单元的")
    _units(memory, item, [(0.0, 10.0)])
    assert extract_clips(memory, Bad(), pstore, item) == []
    assert extract_clips(memory, Boom(), pstore, item) == []
    assert memory.db.clips_for_item(item) == []


def test_auto_process_runs_clip_extraction_only_with_rule(memory, cfg, pstore, monkeypatch):
    """自动流程:只有命中规则的条目才跑关键片段(成本门控)。"""
    from memvault.pipeline import auto as auto_mod

    calls = []

    class StubLLM:
        enabled = True

        def chat_json(self, system, user, **kw):
            if "购物向视频复盘" in system:
                return {"商品": [{"名称": "x"}], "卖点": []}
            return {"clips": []}

    monkeypatch.setattr("memvault.classify.extract_clips",
                        lambda m, llm, ps, iid, cfg=None: calls.append(iid) or [])
    item = memory.add_item("general", "video", "带货视频",
                           attrs={"up": "某UP", "up_mid": "555"})
    memory.add_text_chunk(item, "[单元 00:00-00:10]\n字幕:x", start_ts=0.0, end_ts=10.0)
    # 未绑规则 → 不跑
    auto_mod.auto_process({"item_id": item}, memory, cfg, llm=StubLLM(),
                          pstore=pstore)
    assert calls == []
    # 绑规则 → 跑
    memory.db.bind_prompt("up", "555", "shopping_review")
    auto_mod.auto_process({"item_id": item}, memory, cfg, llm=StubLLM(),
                          pstore=pstore)
    assert calls == [item]
