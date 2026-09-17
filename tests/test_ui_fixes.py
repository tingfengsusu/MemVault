"""面板交互修复的测试:分类建议显示/手动归类/API设置页。"""
import yaml
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client_env(tmp_path, monkeypatch):
    from memvault import llm as llm_mod
    from memvault.config import load_config
    from memvault.server.app import create_app

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump(
        {"llm": {"base_url": "https://old.example/v1", "model": "old-model"}},
        allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("MEMVAULT_CONFIG", str(cfg_file))
    monkeypatch.setattr(llm_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(llm_mod, "_VIDEO2SHOP_CONFIG", tmp_path / "no.yaml")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    cfg = load_config()
    cfg["data_dir"] = str(tmp_path / "data")
    cfg["embedding"]["fake"] = True
    cfg["llm"]["api_key"] = None
    app = create_app(cfg)
    with TestClient(app) as client:
        yield client, app, cfg, cfg_file, tmp_path


def test_inbox_shows_suggestion_and_manual_classify(client_env):
    client, app, *_ = client_env
    mem = app.state.memory
    cat_id = mem.db.add_category("general", "技术笔记")
    item_id = mem.add_item("general", "video", "评测视频", content_text="内容",
                           category_id=cat_id, status="inbox",
                           attrs=None)
    mem.db.set_item_category(item_id, cat_id, 0.85, "测试建议")

    page = client.get("/inbox")
    assert "AI 建议:技术笔记" in page.text  # 显示分类名而非 id

    item2 = mem.add_item("general", "note", "待归类条目", content_text="x")
    r = client.post(f"/items/{item2}/classify", data={"category_id": cat_id},
                    follow_redirects=False)
    assert r.status_code == 303
    it = mem.get_item(item2)
    assert it["status"] == "filed" and it["category_id"] == cat_id

    html = client.get(f"/items/{item2}").text
    assert "分类:技术笔记" in html


def test_settings_save_and_apply(client_env):
    client, app, cfg, cfg_file, tmp_path = client_env
    r = client.post("/settings/save", data={
        "base_url": "https://api.example.com/v1", "model": "my-model",
        "api_key": "sk-test-1234567890abc", "classify_confidence": "0.7"},
        follow_redirects=False)
    assert r.status_code == 303

    saved = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert saved["llm"]["model"] == "my-model"
    assert saved["llm"]["classify_confidence"] == 0.7
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY=sk-test-1234567890abc" in env_text
    # 即时生效:客户端已重建
    assert app.state.llm.model == "my-model"
    assert app.state.llm.api_key == "sk-test-1234567890abc"

    page = client.get("/settings")
    assert page.status_code == 200 and "my-model" in page.text


def test_settings_masks_api_key(client_env):
    client, app, *_ = client_env
    app.state.llm.api_key = "sk-abcdef1234567890xyz"
    page = client.get("/settings")
    assert "sk-abc" in page.text
    assert "1234567890xyz" not in page.text  # 完整 key 不回显
