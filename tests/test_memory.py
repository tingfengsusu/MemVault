from memvault.memory import fmt_ts


def test_add_text_chunk_and_vector_search(memory):
    item_id = memory.add_item("cooking", "video", "红烧肉教程",
                              source_type="video", source_ref="BV1xx")
    memory.add_text_chunk(item_id, "起锅烧油,放入冰糖炒糖色", start_ts=12.5)
    results = memory.search("起锅烧油,放入冰糖炒糖色")  # 同文本 → 假嵌入余弦 1.0
    assert results
    assert results[0]["item"]["id"] == item_id
    assert results[0]["chunk"]["start_ts"] == 12.5


def test_search_keyword_path(memory):
    item_id = memory.add_item("fitness", "video", "卧推教学")
    memory.add_text_chunk(item_id, "今天练胸:平板卧推 5 组,每组 8 次")
    # 假嵌入不会语义匹配,靠 FTS 关键词召回
    results = memory.search("平板卧推")
    assert any(r["item"]["id"] == item_id for r in results)


def test_search_domain_filter(memory):
    fit = memory.add_item("fitness", "video", "卧推")
    cook = memory.add_item("cooking", "video", "炒菜")
    memory.add_text_chunk(fit, "杠铃卧推标准动作讲解")
    memory.add_text_chunk(cook, "杠铃卧推形状的锅?不存在,这句是为了关键词干扰")
    results = memory.search("杠铃卧推", domain="fitness")
    assert results and all(r["item"]["domain"] == "fitness" for r in results)


def test_empty_content_chunk_rejected(memory):
    item_id = memory.add_item("general", "video", "t")
    assert memory.add_text_chunk(item_id, "   ") == 0


def test_image_chunk_stays_pending_without_clip(memory):
    item_id = memory.add_item("general", "video", "t")
    cid = memory.add_image_chunk(item_id, "some/frame.jpg", start_ts=1.0)
    chunk = memory.get_item(item_id)["chunks"][0]
    assert chunk["id"] == cid
    assert chunk["modality"] == "image"
    assert chunk["embed_status"] == "pending"  # 无 CLIP,保持待嵌入


def test_timeline_and_log(memory):
    memory.add_log("今天胸 5 组 + 跑步 40 分钟", domain="fitness")
    rows = memory.timeline(days=7, domain="fitness")
    assert len(rows) == 1
    assert "卧推" not in rows[0]["content_text"]
    assert memory.timeline(days=7, domain="shopping") == []


def test_fmt_ts():
    assert fmt_ts(75.4) == "01:15"
    assert fmt_ts(None) == "--:--"
