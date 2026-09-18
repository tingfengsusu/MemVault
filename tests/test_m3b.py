"""M3b:订阅采集 + 对话式录入。LLM 注入 Stub,B站接口注入 fake fetch。"""
import pytest

from memvault.chat import chat_turn
from memvault.scheduler import check_source, schedule_pass
from memvault.sources import bili_watch


class StubLLM:
    enabled = True

    def __init__(self, json_responses, text_reply="好的,已了解。"):
        self.json_responses = list(json_responses)
        self.text_reply = text_reply
        self.calls = []

    def chat_json(self, system, user, **kw):
        self.calls.append((system, user))
        return self.json_responses.pop(0)

    def chat(self, system, user, **kw):
        self.calls.append((system, user))
        return self.text_reply


# ── B站订阅 ──────────────────────────────────────────────────────────
def test_parse_mid():
    assert bili_watch.parse_mid("space.bilibili.com/12345") == "12345"
    assert bili_watch.parse_mid("https://space.bilibili.com/99?tab=video") == "99"
    assert bili_watch.parse_mid("  12345 ") == "12345"
    assert bili_watch.parse_mid("not-a-uid") is None
    assert bili_watch.parse_mid("") is None


def test_wbi_sign_deterministic():
    img, sub = "a" * 32, "b" * 32  # 真实 key 为 32+32 字符
    p1 = bili_watch.wbi_sign({"mid": "2", "ps": 10}, img, sub, ts=1700000000)
    p2 = bili_watch.wbi_sign({"ps": 10, "mid": "2"}, img, sub, ts=1700000000)
    assert p1 == p2  # 参数排序不影响结果
    assert len(p1["w_rid"]) == 32
    assert p1["wts"] == "1700000000"  # wts 以字符串形式参与签名与发送


def test_check_source_first_run_seeds_history(memory, cfg):
    sid = memory.db.add_watch_source("bili_up", "12345", domain="fitness")
    videos = [{"bvid": f"BV{i}", "title": f"v{i}"} for i in range(3)]
    r = check_source(memory, cfg, sid, fetch=lambda mid, limit: videos)
    assert r == {"new": 0, "seeded": 3, "total": 3}
    # 历史只登记不入库:全部 done,无 pending
    rows = memory.db._conn().execute(
        "SELECT status FROM jobs WHERE type='ingest_video'").fetchall()
    assert {x["status"] for x in rows} == {"done"}


def test_check_source_incremental(memory, cfg):
    sid = memory.db.add_watch_source("bili_up", "12345", domain="fitness")
    check_source(memory, cfg, sid,
                 fetch=lambda mid, limit: [{"bvid": "BVold", "title": "旧"}])
    r = check_source(memory, cfg, sid,
                     fetch=lambda mid, limit: [{"bvid": "BVold", "title": "旧"},
                                               {"bvid": "BVnew", "title": "新"}])
    assert r["new"] == 1
    row = memory.db._conn().execute(
        "SELECT payload FROM jobs WHERE dedup_key='bili|BVnew'").fetchone()
    assert row and "BVnew" in row["payload"]
    # 第三次同样内容 → 不重复
    r2 = check_source(memory, cfg, sid,
                      fetch=lambda mid, limit: [{"bvid": "BVnew", "title": "新"}])
    assert r2["new"] == 0


def test_schedule_pass_bucket_dedup(memory, cfg):
    memory.db.add_watch_source("bili_up", "1")
    memory.db.add_watch_source("bili_up", "2")
    n1 = schedule_pass(memory, cfg, interval_seconds=1800)
    assert n1 == 2
    assert schedule_pass(memory, cfg, interval_seconds=1800) == 0  # 同桶去重
    jobs = memory.db.watch_sources  # 占位避免误用
    rows = memory.db._conn().execute(
        "SELECT COUNT(*) c FROM jobs WHERE type='watch_check'").fetchone()
    assert rows["c"] == 2


def test_worker_handles_watch_check(memory, cfg):
    from memvault.worker import build_dispatch, step

    sid = memory.db.add_watch_source("bili_up", "777")
    memory.db.enqueue("watch_check", {"source_id": sid})
    dispatch = build_dispatch(memory, cfg)
    # fetch 不可注入到 dispatch 里,这里 monkeypatch 模块函数
    from memvault import scheduler as sched

    orig = sched.check_source
    try:
        sched.check_source = lambda m, c, sid_, **kw: {"new": 0}
        assert step(memory, cfg, dispatch=dispatch) is True
    finally:
        sched.check_source = orig
    row = memory.db._conn().execute(
        "SELECT status FROM jobs WHERE type='watch_check'").fetchone()
    assert row["status"] == "done"


# ── 聊天 ─────────────────────────────────────────────────────────────
def test_chat_turn_extracts_and_replies(memory, cfg):
    llm = StubLLM(
        [{"profile": [{"key": "身高", "value": "180"}],
          "log": {"content": "今天练了胸 5 组", "domain": "fitness"},
          "search_query": "卧推训练"},
         {"profile": [], "log": None, "search_query": None}],
        text_reply="明天建议练腿。")
    r = chat_turn(memory, llm, "我身高 180,今天练了胸 5 组,明天练什么?",
                  skill="fitness")
    assert r["reply"] == "明天建议练腿。"
    assert r["profile_saved"] == ["身高"]
    assert r["log_saved"] == "今天练了胸 5 组"
    # 画像与日志已持久化
    assert memory.get_profile("fitness")
    assert memory.timeline(days=7, domain="fitness")
    # 第二次调用应包含个人上下文
    chat_turn(memory, llm, "最近练得怎么样?", skill="fitness")
    system, user = llm.calls[-1]
    assert "画像" in user and "近期日志" in user


def test_chat_turn_no_extraction(memory, cfg):
    llm = StubLLM([{"profile": [], "log": None, "search_query": None}])
    r = chat_turn(memory, llm, "你好呀", skill="general")
    assert r["profile_saved"] == [] and r["log_saved"] is None
    assert memory.get_profile() == []


# ── 面板 ─────────────────────────────────────────────────────────────
@pytest.fixture()
def api(tmp_path, monkeypatch):
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
    cfg["llm"]["api_key"] = None
    app = create_app(cfg)
    with TestClient(app) as client:
        yield client, app, cfg


def test_panel_sources_flow(api):
    """订阅页迁到 Vue 后:外壳 + /api/sources* 接口(旧表单端点双轨保留)。"""
    client, app, _ = api
    page = client.get("/sources")
    assert page.status_code == 200 and 'id="sources-app"' in page.text

    # 旧表单端点仍可用
    r = client.post("/sources/add", data={"kind": "bili_up", "target": "8888",
                                          "domain": "fitness"},
                    follow_redirects=False)
    assert r.status_code == 303
    # 新 JSON 端点
    r2 = client.post("/api/sources", json={"kind": "bili_up", "target": "9999",
                                           "domain": "general"})
    assert r2.json()["data"]["target"] == "9999"
    items = client.get("/api/sources").json()["data"]["items"]
    assert {s["target"] for s in items} == {"8888", "9999"}

    sid = app.state.memory.db.watch_sources()[0]["id"]
    r3 = client.post(f"/api/sources/{sid}/check")
    assert r3.json()["data"]["queued"] == "watch_check"
    row = app.state.memory.db._conn().execute(
        "SELECT type FROM jobs WHERE type='watch_check'").fetchone()
    assert row is not None

    assert client.post(f"/api/sources/{sid}/toggle").json()["data"]["enabled"] in (0, False)
    assert len(app.state.memory.db.watch_sources(enabled_only=True)) == 1
    assert client.post("/api/sources/99999/toggle").status_code == 404
    assert client.post("/api/sources/99999/check").status_code == 404
    assert client.post("/api/sources", json={"kind": "bili_up",
                                             "target": "不是UID"}).status_code == 422


def test_panel_chat_llm_disabled(api):
    client, _, _ = api
    r = client.post("/api/chat", json={"message": "你好"})
    assert r.status_code == 503
    assert client.get("/chat").status_code == 200


def test_panel_chat_with_stub(api):
    client, app, _ = api
    from tests.test_m3b import StubLLM

    app.state.llm = StubLLM(
        [{"profile": [], "log": None, "search_query": None}],
        text_reply="收到!")
    r = client.post("/api/chat", json={"message": "在吗", "skill": "general"})
    assert r.status_code == 200
    assert r.json()["reply"] == "收到!"


def test_get_up_latest_fallback(monkeypatch):
    """wbi 主通道被风控时自动降级 series 通道。"""
    from memvault.sources import bili_watch

    monkeypatch.setattr(bili_watch, "_session", lambda p=None: object())
    monkeypatch.setattr(bili_watch, "_get_latest_wbi",
                        lambda s, mid, limit: (_ for _ in ()).throw(
                            RuntimeError("wbi 通道 code=-352: 风控校验失败")))
    monkeypatch.setattr(bili_watch, "_get_latest_series",
                        lambda s, mid, limit: [{"bvid": "BV1x", "title": "t",
                                                "created": 1}])
    videos = bili_watch.get_up_latest("123", 5)
    assert videos[0]["bvid"] == "BV1x"
