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


def test_delete_items_cancels_item_jobs(memory):
    """同源重跑清理旧条目时,指向它的待执行任务一并作废。

    否则残留的 auto_process 在此后执行时报"条目不存在"(真实 job#134)。
    """
    old = memory.add_item("general", "video", "旧视频", source_ref="BV1same")
    memory.db.enqueue("auto_process", {"item_id": old}, dedup_key=f"auto|{old}")
    memory.db.enqueue("build_links", {"item_id": old}, dedup_key=f"links|{old}")
    other = memory.add_item("general", "note", "别的条目")
    other_job = memory.db.enqueue("auto_process", {"item_id": other})

    ids = memory.db.delete_items_by_source_ref("BV1same")

    assert ids == [old]
    rows = memory.db._conn().execute("SELECT id, status FROM jobs").fetchall()
    assert [r["id"] for r in rows] == [other_job]  # 只留其他条目自己的任务
    assert rows[0]["status"] == "pending"


def test_fts_search_chinese(memory):
    item_id = memory.add_item("cooking", "video", "红烧肉")
    cid = memory.db.add_chunk(item_id, "text", content="这一步要加入两勺生抽提鲜")
    hits = memory.db.fts_search_chunks("生抽", limit=10)
    assert cid in hits


def test_delete_items_cleans_fts(memory):
    """删除条目要同步清 FTS 行,否则关键词检索会命中幽灵语义块。"""
    item_id = memory.add_item("general", "video", "旧视频", source_ref="BV1x")
    cid = memory.db.add_chunk(item_id, "text", content="这里有一句独特的检索关键词")
    assert cid in memory.db.fts_search_chunks("独特的检索关键词")

    memory.db.delete_items_by_source_ref("BV1x")

    conn = memory.db._conn()
    assert conn.execute("SELECT COUNT(*) FROM items_fts WHERE item_id=?",
                        (item_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunk_id=?",
                        (cid,)).fetchone()[0] == 0


def test_startup_purges_stale_and_duplicate_fts(tmp_path):
    """老库遗留的幽灵/重复 FTS 行在启动时清掉(真实库 items_fts 17 行 vs 12 条目)。"""
    from memvault.db import Database

    path = tmp_path / "fts.db"
    db = Database(path)
    item_id = db.add_item("general", "note", "标题", content_text="正文")
    db.add_chunk(item_id, "text", content="块内容")
    conn = db._conn()
    conn.execute("INSERT INTO items_fts(title, content_text, item_id)"
                 " VALUES('幽灵标题','',999)")           # 指向已不存在的条目
    conn.execute("INSERT INTO items_fts(title, content_text, item_id)"
                 " VALUES('重复标题','',?)", (item_id,))  # 同一 item 的重复行
    conn.execute("INSERT INTO chunks_fts(content, chunk_id) VALUES('幽灵块',888)")
    conn.commit()

    assert db._purge_stale_fts(conn) == 3
    assert conn.execute("SELECT COUNT(*) FROM items_fts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 1
    # 启动路径也生效(= 重新打开同一个库)
    assert Database(path)._conn().execute(
        "SELECT COUNT(*) FROM items_fts").fetchone()[0] == 1


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


def test_recover_stale_jobs(memory):
    from memvault.worker import recover_stale_jobs

    j1 = memory.db.enqueue("ingest_video", {"source": "BV1"})
    j2 = memory.db.enqueue("ingest_text", {"text": "x"})
    memory.db.claim_next()                      # j1 → running
    memory.db.claim_next()                      # j2 → running
    n = recover_stale_jobs(memory.db)
    assert n == 2
    rows = memory.db._conn().execute(
        "SELECT status, error FROM jobs ORDER BY id").fetchall()
    assert all(r["status"] == "pending" for r in rows)
    assert "中断" in rows[0]["error"]
