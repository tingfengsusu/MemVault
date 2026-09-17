"""提速相关工具测试: ASR 段合并 / 批量嵌入。"""


def test_merge_asr_segments():
    from memvault.pipeline.video import _merge_asr_segments

    segs = [{"start": i * 5.0, "end": i * 5.0 + 5, "text": f"第{i}句。"}
            for i in range(30)]
    merged = _merge_asr_segments(segs, max_chars=60, max_seconds=20)
    assert 5 <= len(merged) < 15               # 明显合并
    assert merged[0]["start"] == 0.0
    assert merged[-1]["end"] == segs[-1]["end"]
    total = "".join(m["text"] for m in merged)
    assert total.count("句") == 30             # 内容无丢失
    assert _merge_asr_segments([]) == []


def test_add_text_chunks_batch(memory):
    item = memory.add_item("general", "note", "T", content_text="x")
    n = memory.add_text_chunks_batch(item, [
        {"content": "块一内容", "start_ts": 1.0, "seq": 0},
        {"content": "块二内容", "seq": 1},
        {"content": "   ", "seq": 2},          # 空白块被过滤
    ])
    assert n == 2
    chunks = memory.get_item(item)["chunks"]
    assert len(chunks) == 2
    assert all(c["embed_status"] == "done" for c in chunks)
    assert memory.search("块一内容")           # 已进向量库
