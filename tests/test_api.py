import pytest
from fastapi.testclient import TestClient

from memvault.config import load_config
from memvault.server.app import create_app
from memvault.worker import step


@pytest.fixture()
def env(tmp_path):
    cfg = load_config()
    cfg["data_dir"] = str(tmp_path)
    cfg["embedding"]["fake"] = True
    app = create_app(cfg)
    with TestClient(app) as client:
        yield client, app, cfg


def test_health(env):
    client, app, _ = env
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["paused"] is False


def test_capture_selection_end_to_end(env):
    client, app, cfg = env
    r = client.post("/api/capture", json={
        "type": "selection", "url": "https://example.com/post/1",
        "title": "训练计划贴", "text": "今天练胸:平板卧推5组,每组8次",
    })
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    processed = step(app.state.memory, cfg)  # 手动驱动 worker 一轮
    assert processed is True

    items = app.state.memory.db.list_items()
    assert len(items) == 1
    assert items[0]["type"] == "note"
    assert items[0]["status"] == "inbox"

    page = client.get("/search", params={"q": "平板卧推"})
    assert page.status_code == 200
    assert "训练计划贴" in page.text

    row = app.state.memory.db._conn().execute(
        "SELECT status FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    assert row["status"] == "done"


def test_capture_dedup_same_job(env):
    client, _, _ = env
    body = {"type": "selection", "url": "https://x.com/a", "text": "同一段文字去重测试"}
    id1 = client.post("/api/capture", json=body).json()["job_id"]
    id2 = client.post("/api/capture", json=body).json()["job_id"]
    assert id1 == id2


def test_capture_product(env):
    client, app, cfg = env
    r = client.post("/api/capture", json={
        "type": "product", "url": "https://item.jd.com/10001.html",
        "product": {"name": "优衣库摇粒绒外套", "price": "199元",
                    "shop": "京东"},
    })
    assert r.status_code == 200
    step(app.state.memory, cfg)

    items = app.state.memory.db.list_items()
    assert items[0]["type"] == "product"
    assert items[0]["domain"] == "shopping"
    assert items[0]["title"] == "优衣库摇粒绒外套"
    assert "199元" in items[0]["attrs_json"]

    page = client.get("/search", params={"q": "优衣库摇粒绒"})
    assert "优衣库摇粒绒外套" in page.text


def test_capture_video_enqueues_only(env):
    """视频采集只入队不执行(执行会真实下载)。"""
    client, app, _ = env
    body = {
        "type": "video", "url": "https://www.bilibili.com/video/BV1xx411c7mD",
        "video": {"platform": "bilibili", "id": "BV1xx411c7mD", "t": 42},
    }
    id1 = client.post("/api/capture", json=body).json()["job_id"]
    id2 = client.post("/api/capture", json=body).json()["job_id"]
    assert id1 == id2

    row = app.state.memory.db._conn().execute(
        "SELECT type, status, payload FROM jobs WHERE id=?", (id1,)
    ).fetchone()
    assert row["type"] == "ingest_video"
    assert row["status"] == "pending"
    assert "BV1xx411c7mD" in row["payload"]


def test_pause_blocks_capture(env):
    client, _, _ = env
    assert client.post("/api/pause").json()["paused"] is True
    r = client.post("/api/capture", json={"type": "page", "text": "x" * 20})
    assert r.status_code == 403
    assert client.post("/api/pause").json()["paused"] is False


def test_capture_validation(env):
    client, _, _ = env
    assert client.post("/api/capture",
                       json={"type": "page"}).status_code == 422
    assert client.post("/api/capture", json={
        "type": "video"}).status_code == 422


def test_panel_pages(env):
    client, app, cfg = env
    step_ok = None

    r = client.post("/api/capture", json={
        "type": "page", "url": "https://example.com",
        "title": "示例页面", "text": "这是一段示例页面正文内容",
    })
    job_id = r.json()["job_id"]
    step_ok = step(app.state.memory, cfg)
    assert step_ok

    assert client.get("/").status_code == 200
    assert "示例页面" in client.get("/").text
    inbox = client.get("/inbox")
    assert inbox.status_code == 200 and "示例页面" in inbox.text
    assert client.get("/jobs").status_code == 200

    item_id = app.state.memory.db.list_items()[0]["id"]
    detail = client.get(f"/items/{item_id}")
    assert detail.status_code == 200 and "示例页面" in detail.text
    assert client.get("/items/99999").status_code == 404

    # 状态流转:inbox → filed(TestClient 默认跟随重定向,这里关掉验证 303)
    r = client.post(f"/items/{item_id}/status", params={"status": "filed"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert app.state.memory.get_item(item_id)["status"] == "filed"
    assert client.post(f"/items/{item_id}/status",
                       params={"status": "bad"}).status_code == 422


def test_item_detail_bili_jump(env):
    client, app, cfg = env
    # 直接构造带 B站来源与时间戳的条目(不走下载)
    mem = app.state.memory
    item_id = mem.add_item("cooking", "video", "红烧肉教程",
                           source_type="video",
                           source_ref="https://www.bilibili.com/video/BV1xx411c7mD")
    mem.add_text_chunk(item_id, "起锅烧油放入冰糖", start_ts=75.0)

    html = client.get(f"/items/{item_id}").text
    assert "01:15" in html
    assert "https://www.bilibili.com/video/BV1xx411c7mD?t=75" in html
