"""/api/* JSON 层测试(方案 A 第 0 步):响应契约、分页、过滤、错误形状。

契约:成功 {ok:true, data:..., error:null};失败 {ok:false, error:{code,message}}。
同时确认**旧页面路由不受影响**(双轨并存)。
"""
import pytest


@pytest.fixture()
def api(tmp_path):
    from fastapi.testclient import TestClient

    from memvault.config import load_config
    from memvault.server.app import create_app

    cfg = load_config()
    cfg["data_dir"] = str(tmp_path / "data")
    cfg["embedding"]["fake"] = True
    cfg["llm"] = dict(cfg["llm"], api_key=None)
    app = create_app(cfg)
    with TestClient(app) as client:
        yield client, app


def _seed(app):
    m = app.state.memory
    cat_id = m.db.add_category("fitness", "胸部训练")
    a = m.add_item("fitness", "video", "卧推讲解", content_text="平板卧推要点",
                   category_id=cat_id, status="filed")
    b = m.add_item("cooking", "video", "红烧肉", content_text="小火炖四十分钟",
                   status="inbox")
    c = m.add_item("fitness", "note", "随记", content_text="今天练胸", status="inbox")
    m.add_text_chunk(a, "平板卧推肩胛后收")
    m.add_text_chunk(b, "五花肉焯水后小火炖")
    return m, cat_id, a, b, c


def test_contract_shape_and_stats(api):
    client, app = api
    m, cat_id, a, b, c = _seed(app)

    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["error"] is None
    assert body["data"]["items"] == 3
    assert {d["domain"] for d in body["data"]["domains"]} == {"fitness", "cooking"}


def test_items_list_pagination_and_filters(api):
    client, app = api
    m, cat_id, a, b, c = _seed(app)

    r = client.get("/api/items", params={"page_size": 2})
    d = r.json()["data"]
    assert d["total"] == 3 and len(d["items"]) == 2 and d["page_size"] == 2
    assert d["items"][0]["id"] > d["items"][1]["id"]        # 新的在前

    r2 = client.get("/api/items", params={"page": 2, "page_size": 2})
    assert len(r2.json()["data"]["items"]) == 1

    only_inbox = client.get("/api/items", params={"status": "inbox"}).json()["data"]
    assert only_inbox["total"] == 2
    only_fitness = client.get("/api/items", params={"domain": "fitness"}).json()["data"]
    assert only_fitness["total"] == 2
    assert all(i["domain"] == "fitness" for i in only_fitness["items"])

    by_cat = client.get("/api/items", params={"category_id": cat_id}).json()["data"]
    assert [i["id"] for i in by_cat["items"]] == [a]
    assert by_cat["items"][0]["category_name"] == "胸部训练"

    # page_size 上限保护
    big = client.get("/api/items", params={"page_size": 9999}).json()["data"]
    assert big["page_size"] == 100


def test_items_search_returns_hit_chunk(api):
    client, app = api
    m, cat_id, a, b, c = _seed(app)
    d = client.get("/api/items", params={"q": "卧推"}).json()["data"]
    assert d["items"], "检索应命中卧推条目"
    assert d["query"] == "卧推"
    hit = d["items"][0]
    assert hit["id"] == a
    assert "hit_chunk" in hit and hit["hit_chunk"]["content"]


def test_item_detail_shape(api):
    client, app = api
    m, cat_id, a, b, c = _seed(app)
    d = client.get(f"/api/items/{a}").json()["data"]
    assert d["title"] == "卧推讲解"
    assert d["category_name"] == "胸部训练"
    assert d["attrs_ai"] == {} and d["attrs_raw"] == {}
    assert d["chunks"] and d["chunks"][0]["modality"] == "text"
    assert d["chunk_count"] == len(d["chunks"])          # 详情页也要有块数
    assert d["up"] == {"up_mid": None, "up_name": None, "rule_category_id": None}


def test_item_status_change_and_errors(api):
    client, app = api
    m, cat_id, a, b, c = _seed(app)

    r = client.post(f"/api/items/{b}/status", json={"status": "filed"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["data"]["status"] == "filed"

    # 非法状态 → 统一失败契约(不是 FastAPI 默认的 {"detail": ...})
    bad = client.post(f"/api/items/{b}/status", json={"status": "wat"})
    assert bad.status_code == 422
    body = bad.json()
    assert body["ok"] is False and body["data"] is None
    assert body["error"]["code"] == "invalid_status"

    missing = client.get("/api/items/9999")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_inbox_endpoint_with_categories(api):
    client, app = api
    m, cat_id, a, b, c = _seed(app)
    d = client.get("/api/inbox").json()["data"]
    assert d["total"] == 2
    assert {i["id"] for i in d["items"]} == {b, c}
    # 前端下拉要的分类树(按领域分组)一并给出
    assert "fitness" in d["categories"]
    assert d["categories"]["fitness"][0]["name"] == "胸部训练"


def test_inbox_batch_actions(api):
    client, app = api
    m, cat_id, a, b, c = _seed(app)

    # 指定 ids 归档
    r = client.post("/api/inbox/batch", json={"action": "filed", "ids": [b]})
    assert r.json()["data"]["affected"] == 1
    assert m.get_item(b)["status"] == "filed"
    assert m.get_item(c)["status"] == "inbox"

    # assign:指定分类并归档
    r2 = client.post("/api/inbox/batch",
                     json={"action": "assign", "ids": [c], "category_id": cat_id})
    assert r2.json()["data"]["affected"] == 1
    assert m.get_item(c)["category_id"] == cat_id
    assert m.get_item(c)["status"] == "filed"

    # auto:入队(不真的跑 LLM)
    d = m.add_item("fitness", "video", "待分类", content_text="x")
    r3 = client.post("/api/inbox/batch", json={"action": "auto", "ids": [d]})
    assert r3.json()["data"]["affected"] == 1
    n = m.db._conn().execute(
        "SELECT COUNT(*) FROM jobs WHERE type='auto_process'").fetchone()[0]
    assert n == 1

    # 未知动作 / assign 缺分类 → 422 统一契约
    assert client.post("/api/inbox/batch", json={"action": "wat"}).status_code == 422
    assert client.post("/api/inbox/batch",
                       json={"action": "assign"}).status_code == 422


def test_categories_jobs_sources_settings(api):
    client, app = api
    m, cat_id, a, b, c = _seed(app)

    cats = client.get("/api/categories").json()["data"]
    assert cats["items"][0]["name"] == "胸部训练"
    assert cats["items"][0]["item_count"] == 1
    assert "fitness" in cats["groups"]

    m.db.enqueue("noop", {"x": 1})
    jobs = client.get("/api/jobs").json()["data"]
    assert jobs["items"] and jobs["items"][0]["payload_pretty"] == "x=1"

    m.db.add_watch_source("up", "12345", domain="general")
    src = client.get("/api/sources").json()["data"]
    assert src["items"][0]["target"] == "12345"

    st = client.get("/api/settings").json()["data"]
    # 密钥只回显"是否已配置"(本机 .env 里可能真有 key,所以只断言类型)
    assert isinstance(st["llm"]["api_key_set"], bool)
    assert "api_key" not in st["llm"]                 # 绝不回显明文
    assert "web" not in st["llm"]                     # 内部结构不外泄
    assert "vision" in st and "frames" in st


def test_old_pages_still_work(api):
    """双轨并存:JSON 层不影响原页面路由。"""
    client, app = api
    m, cat_id, a, b, c = _seed(app)
    # 已迁 Vue 的页面断言"外壳"(标题仍在 <title> 里,便于标签页识别);
    # 未迁的页面仍是完整 Jinja2 渲染。
    for path, marker in (("/", "库 · MemVault"), ("/inbox", "待整理"),
                         ("/categories", "分类 · MemVault"), ("/jobs", "任务"),
                         (f"/items/{a}", "卧推讲解"), ("/sources", "订阅"),
                         ("/settings", "设置")):
        r = client.get(path)
        assert r.status_code == 200, path
        assert marker in r.text, path
    assert client.get("/search?q=卧推").status_code == 200


def test_api_error_handler_does_not_leak_into_pages(api):
    """页面路由的 404 仍是原生形状(不套 JSON 契约)。"""
    client, app = api
    r = client.get("/items/9999")
    assert r.status_code == 404
    assert "detail" in r.json()          # FastAPI 默认形状
    assert "ok" not in r.json()
