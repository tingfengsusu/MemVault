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


def test_links_max_chunk_noise_is_damped(memory, cfg):
    """偶发撞词不该建链:旧口径取"任意单块最大值",新口径取 top-3 均值。

    真实证据:#11(全球经济) × #13(是,大臣) 最高单块 0.64,条目级只有 0.51。
    """
    from memvault.links import _top_mean, build_links_for_item

    assert _top_mean([0.9]) == pytest.approx(0.9)
    assert _top_mean([0.9, 0.6, 0.3, 0.1]) == pytest.approx(0.6)  # 取前 3 平均
    assert _top_mean([]) == 0.0

    a = memory.add_item("general", "video", "源头条目", content_text="共同的那段话")
    memory.add_text_chunk(a, "共同的那段话")
    # 只有一段撞词:旧口径会凭这一块入选,新口径被 top-3 均值摊平
    b = memory.add_item("general", "video", "偶发撞词")
    memory.add_text_chunk(b, "共同的那段话")
    memory.add_text_chunk(b, "完全无关的第一段内容")
    memory.add_text_chunk(b, "完全无关的第二段内容")
    memory.add_text_chunk(b, "完全无关的第三段内容")
    # 整条都是同一主题
    c = memory.add_item("general", "video", "真同主题")
    for _ in range(4):
        memory.add_text_chunk(c, "共同的那段话")

    scores = dict(build_links_for_item(memory, a, cfg))
    assert c in scores                     # 整条同主题 → 建链
    assert b not in scores                 # 单块撞词 → 不入链
    assert scores[c] > 0.9                 # 全同文本 → 接近 1.0


def test_profile_text_prefers_ai_summary():
    """比较文本优先用 AI 摘要(干净),而不是原始 ASR 正文(口语填充词是噪音)。"""
    from memvault.links import item_profile_text

    item = {"title": "某视频", "content_text": "朋友们,对吧,所以说,就是就是,嗯嗯",
            "attrs_ai": '{"主题": "冰淇淋教学", "核心内容": "三步做水果冰淇淋"}'}
    text = item_profile_text(item)
    assert "冰淇淋教学" in text and "三步做水果冰淇淋" in text
    assert "朋友们" not in text

    item2 = {"title": "某视频", "content_text": "正文内容", "attrs_ai": "{}"}
    assert item_profile_text(item2) == "正文内容"      # 没有 AI 摘要时退回正文
    assert item_profile_text({"title": "只有标题", "attrs_ai": None}) == "只有标题"


def test_default_threshold_is_calibrated():
    """阈值按真实库分布定(旧 0.55 在真实 ASR 文本上会连出无关条目)。"""
    from memvault.config import DEFAULTS

    assert DEFAULTS["links"]["similarity_threshold"] >= 0.6
    assert DEFAULTS["links"]["candidate_chunks"] >= 20


def test_relink_does_not_wipe_other_side_claim(memory, cfg):
    """后算的条目"没找到对方"时,不得抹掉先算出来的链接。

    真实数据:#8 算出与 #11 的相关(0.656)后,#11 自己的计算没找到 #8,
    链接整条消失(旧 set_links 会删掉"触及该条目的所有行")。
    """
    from memvault.links import build_links_for_item

    a = memory.add_item("general", "note", "A", content_text="同一段内容")
    b = memory.add_item("general", "note", "B", content_text="同一段内容")
    for iid in (a, b):
        memory.add_text_chunk(iid, "同一段内容")

    build_links_for_item(memory, a, cfg)          # a → b 建链
    assert memory.db.links_for_items([a])[a]       # 已建

    # b 重算但"什么都没找到"(模拟长视频自身块占满候选/阈值未过)
    memory.db.set_links(b, [])
    left = memory.db.links_for_items([a])[a]
    assert left and left[0]["id"] == b             # a 的声明还在


def test_own_chunks_do_not_block_candidates(memory, cfg):
    """条目自己的块不能占满候选池,否则永远找不到相关条目(真实 #11 有 67 块)。"""
    from memvault.links import build_links_for_item

    other = memory.add_item("general", "note", "邻居", content_text="共同主题的句子")
    memory.add_text_chunk(other, "共同主题的句子")

    big = memory.add_item("general", "note", "块很多的条目",
                          content_text="共同主题的句子")
    for _ in range(50):                    # 自己 50 块,远超候选池
        memory.add_text_chunk(big, "共同主题的句子")

    ids = [r for r, _ in build_links_for_item(memory, big, cfg)]
    assert other in ids


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
