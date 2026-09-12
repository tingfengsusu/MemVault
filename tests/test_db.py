from memvault.worker import run_worker


def test_item_chunk_roundtrip(memory):
    item_id = memory.add_item("cooking", "video", "红烧肉教程", source_type="video")
    memory.db.add_chunk(item_id, "text", content="先焯水的目的是去腥", seq=1)
    item = memory.get_item(item_id)
    assert item["title"] == "红烧肉教程"
    assert len(item["chunks"]) == 1
    assert item["chunks"][0]["content"] == "先焯水的目的是去腥"


def test_jobs_dedup(memory):
    j1 = memory.db.enqueue("ingest_video", {"source": "BV1xx"}, dedup_key="bv1xx")
    j2 = memory.db.enqueue("ingest_video", {"source": "BV1xx"}, dedup_key="bv1xx")
    assert j1 == j2


def test_jobs_claim_and_finish(memory):
    memory.db.enqueue("ingest_video", {"source": "BV1xx"}, dedup_key="k1")
    job = memory.db.claim_next()
    assert job["status"] == "running"
    memory.db.finish_job(job["id"], ok=True)
    again = memory.db.claim_next()
    assert again is None  # 已完成,无新任务


def test_fts_search_chinese(memory):
    item_id = memory.add_item("cooking", "video", "红烧肉")
    cid = memory.db.add_chunk(item_id, "text", content="这一步要加入两勺生抽提鲜")
    hits = memory.db.fts_search_chunks("生抽", limit=10)
    assert cid in hits


def test_profile_upsert(memory):
    memory.set_profile("身高", 178)
    memory.set_profile("身高", 180)  # 更新而非新增
    rows = memory.get_profile()
    assert len(rows) == 1
    assert rows[0]["value_json"] == "180"


def test_update_item_media(memory):
    item_id = memory.add_item("general", "video", "t")
    memory.db.update_item_media(item_id, content_text="汇总", media_paths=["a.jpg"])
    item = memory.get_item(item_id)
    assert item["content_text"] == "汇总"
    assert item["media_paths"] == '["a.jpg"]'


def test_worker_processes_job(memory):
    done = []
    memory.db.enqueue("noop", {"x": 1})
    run_worker(memory, {}, poll_seconds=0, stop=lambda: len(done) >= 1,
               dispatch={"noop": lambda p: done.append(p)})
    assert done == [{"x": 1}]
    row = memory.db._conn().execute(
        "SELECT status FROM jobs WHERE type='noop'"
    ).fetchone()
    assert row["status"] == "done"


def test_worker_marks_unknown_type_failed(memory):
    memory.db.enqueue("mystery", {})

    def stop():
        row = memory.db._conn().execute(
            "SELECT status FROM jobs WHERE type='mystery'"
        ).fetchone()
        return row is not None and row["status"] in ("done", "failed")

    run_worker(memory, {}, poll_seconds=0, stop=stop, dispatch={})
    row = memory.db._conn().execute(
        "SELECT status, error FROM jobs WHERE type='mystery'"
    ).fetchone()
    assert row["status"] == "failed"
    assert "未知任务类型" in row["error"]
