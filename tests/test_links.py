"""双链(相关条目)测试:相似度建链、幂等、双向查询、页面展示。"""
import pytest


def test_links_created_for_similar_items(memory, cfg):
    from memvault.links import build_links_for_item

    a = memory.add_item("fitness", "note", "卧推技巧",
                        content_text="平板卧推肩胛后收,杠铃落点乳头连线")
    b = memory.add_item("fitness", "note", "卧推技巧",
                        content_text="平板卧推肩胛后收,杠铃落点乳头连线")
    c = memory.add_item("cooking", "note", "红烧肉",
                        content_text="五花肉焯水,冰糖炒糖色,小火炖40分钟")
    for iid in (a, b, c):
        memory.add_text_chunk(iid, memory.get_item(iid)["content_text"])

    related = build_links_for_item(memory, a, cfg)
    ids = [r for r, _ in related]
    assert b in ids          # 同文本 → 相似度 1.0 建链
    assert c not in ids      # 无关内容(假嵌入下近似正交)不建链


def test_links_idempotent(memory, cfg):
    from memvault.links import build_links_for_item

    a = memory.add_item("general", "note", "同主题", content_text="完全一样的内容")
    b = memory.add_item("general", "note", "同主题2", content_text="完全一样的内容")
    memory.add_text_chunk(a, "完全一样的内容")
    memory.add_text_chunk(b, "完全一样的内容")
    build_links_for_item(memory, a, cfg)
    build_links_for_item(memory, a, cfg)  # 重复计算
    n = memory.db._conn().execute("SELECT COUNT(*) c FROM links").fetchone()["c"]
    assert n == 1  # 整体替换,不累积


def test_links_bidirectional_query(memory, cfg):
    from memvault.links import build_links_for_item

    a = memory.add_item("general", "note", "A", content_text="共享同一段文字")
    b = memory.add_item("general", "note", "B", content_text="共享同一段文字")
    memory.add_text_chunk(a, "共享同一段文字")
    memory.add_text_chunk(b, "共享同一段文字")
    build_links_for_item(memory, a, cfg)

    la = memory.db.links_for_items([a])[a]
    lb = memory.db.links_for_items([b])[b]   # 反向也能查到
    assert la[0]["id"] == b and lb[0]["id"] == a
    assert la[0]["title"] == "B"


def test_library_page_shows_related(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from memvault import llm as llm_mod
    from memvault.config import load_config
    from memvault.links import build_links_for_item
    from memvault.server.app import create_app

    monkeypatch.setattr(llm_mod, "_VIDEO2SHOP_CONFIG", tmp_path / "no.yaml")
    monkeypatch.setattr(llm_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = load_config()
    cfg["data_dir"] = str(tmp_path / "data")
    cfg["embedding"]["fake"] = True
    cfg["llm"] = dict(cfg["llm"], api_key=None)
    app = create_app(cfg)
    with TestClient(app) as client:
        mem = app.state.memory
        a = mem.add_item("fitness", "note", "深蹲要点", content_text="膝盖与脚尖同向")
        b = mem.add_item("fitness", "note", "深蹲要点二", content_text="膝盖与脚尖同向")
        mem.add_text_chunk(a, "膝盖与脚尖同向")
        mem.add_text_chunk(b, "膝盖与脚尖同向")
        build_links_for_item(mem, a, cfg)

        page = client.get("/")
        assert "🔗 相关:" in page.text and "深蹲要点二" in page.text

        detail = client.get(f"/items/{a}").text
        assert "相关条目" in detail and "相似度 1.00" in detail
