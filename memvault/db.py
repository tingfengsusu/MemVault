"""SQLite 存储层 — 唯一事实源。

设计见 DESIGN.md §2:items/chunks/categories/profile/logs/jobs/prompts/
prompt_feedback/watch_sources 九张表;FTS5(trigram)支持中文全文检索,
环境不支持时自动降级为 LIKE。
"""
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id            INTEGER PRIMARY KEY,
  domain        TEXT NOT NULL,
  type          TEXT NOT NULL,
  title         TEXT NOT NULL,
  attrs_json    TEXT DEFAULT '{}',
  content_text  TEXT,
  media_paths   TEXT DEFAULT '[]',
  source_type   TEXT,
  source_ref    TEXT,
  category_id   INTEGER REFERENCES categories(id),
  status        TEXT DEFAULT 'inbox',
  created_at    TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS chunks (
  id           INTEGER PRIMARY KEY,
  item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  modality     TEXT NOT NULL,
  content      TEXT,
  media_path   TEXT,
  start_ts     REAL,
  end_ts       REAL,
  seq          INTEGER,
  embed_status TEXT DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_chunks_item ON chunks(item_id);

CREATE TABLE IF NOT EXISTS categories (
  id        INTEGER PRIMARY KEY,
  domain    TEXT NOT NULL,
  parent_id INTEGER REFERENCES categories(id),
  name      TEXT NOT NULL,
  sort      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS profile (
  id         INTEGER PRIMARY KEY,
  domain     TEXT DEFAULT 'general',
  key        TEXT NOT NULL,
  value_json TEXT NOT NULL,
  source     TEXT,
  updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS logs (
  id               INTEGER PRIMARY KEY,
  domain           TEXT,
  happened_at      TEXT NOT NULL,
  content_text     TEXT NOT NULL,
  attrs_json       TEXT DEFAULT '{}',
  related_item_ids TEXT DEFAULT '[]',
  source           TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  id         INTEGER PRIMARY KEY,
  type       TEXT NOT NULL,
  payload    TEXT NOT NULL,
  status     TEXT DEFAULT 'pending',
  dedup_key  TEXT UNIQUE,
  priority   INTEGER DEFAULT 0,
  error      TEXT,
  created_at TEXT DEFAULT (datetime('now', 'localtime')),
  started_at TEXT,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS prompts (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  stage       TEXT NOT NULL,
  category_id INTEGER REFERENCES categories(id),
  version     INTEGER NOT NULL DEFAULT 1,
  content     TEXT NOT NULL,
  status      TEXT DEFAULT 'active',
  origin      TEXT DEFAULT 'system',
  created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS prompt_feedback (
  id           INTEGER PRIMARY KEY,
  prompt_id    INTEGER REFERENCES prompts(id),
  item_id      INTEGER REFERENCES items(id),
  critique     TEXT NOT NULL,
  dimension    TEXT,
  action_taken TEXT,
  created_at   TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS watch_sources (
  id           INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL,
  target       TEXT NOT NULL,
  domain       TEXT,
  enabled      INTEGER DEFAULT 1,
  last_checked TEXT,
  created_at   TEXT DEFAULT (datetime('now', 'localtime'))
);
"""


class Database:
    """线程安全封装:每线程一个连接,WAL 模式。"""

    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.fts_enabled = True
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)
            try:
                c.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                    "USING fts5(content, chunk_id UNINDEXED, tokenize='trigram')"
                )
                c.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS items_fts "
                    "USING fts5(title, content_text, item_id UNINDEXED, tokenize='trigram')"
                )
            except sqlite3.OperationalError:
                self.fts_enabled = False

    def _migrate(self, conn):
        """轻量列迁移:老库补新列。"""

        def has_col(table, col):
            return any(r["name"] == col for r in conn.execute(f"PRAGMA table_info({table})"))

        if not has_col("categories", "status"):
            conn.execute("ALTER TABLE categories ADD COLUMN status TEXT DEFAULT 'active'")
        if not has_col("items", "category_conf"):
            conn.execute("ALTER TABLE items ADD COLUMN category_conf REAL")
        if not has_col("items", "auto_note"):
            conn.execute("ALTER TABLE items ADD COLUMN auto_note TEXT")
        if not has_col("watch_sources", "label"):
            conn.execute("ALTER TABLE watch_sources ADD COLUMN label TEXT")
        # 旧数据日志时间是 ISO 'T' 分隔,与 SQLite datetime() 的空格格式混用
        n = conn.execute(
            "UPDATE logs SET happened_at = REPLACE(happened_at, 'T', ' ')"
            " WHERE happened_at LIKE '%T%'"
        ).rowcount
        if n:
            logger = __import__("logging").getLogger(__name__)
            logger.info("迁移:归一化 %d 条日志时间格式", n)
        conn.commit()

    # ── 连接管理 ──────────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @staticmethod
    def _rows(cur) -> list[dict]:
        return [dict(r) for r in cur.fetchall()]

    # ── items / chunks ────────────────────────────────────────────────
    def add_item(self, domain, type_, title, attrs=None, content_text=None,
                 media_paths=None, source_type=None, source_ref=None,
                 category_id=None, status="inbox") -> int:
        cur = self._conn().execute(
            "INSERT INTO items(domain, type, title, attrs_json, content_text,"
            " media_paths, source_type, source_ref, category_id, status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (domain, type_, title, json.dumps(attrs or {}, ensure_ascii=False),
             content_text, json.dumps(media_paths or [], ensure_ascii=False),
             source_type, source_ref, category_id, status),
        )
        item_id = cur.lastrowid
        self._conn().execute(
            "INSERT INTO items_fts(title, content_text, item_id) VALUES(?,?,?)",
            (title, content_text or "", item_id),
        )
        self._conn().commit()
        return item_id

    def add_chunk(self, item_id, modality, content=None, media_path=None,
                  start_ts=None, end_ts=None, seq=None) -> int:
        cur = self._conn().execute(
            "INSERT INTO chunks(item_id, modality, content, media_path,"
            " start_ts, end_ts, seq) VALUES(?,?,?,?,?,?,?)",
            (item_id, modality, content, media_path, start_ts, end_ts, seq),
        )
        chunk_id = cur.lastrowid
        if modality == "text" and content:
            self._conn().execute(
                "INSERT INTO chunks_fts(content, chunk_id) VALUES(?,?)",
                (content, chunk_id),
            )
        self._conn().commit()
        return chunk_id

    def get_chunks(self, chunk_ids: list[int]) -> list[dict]:
        if not chunk_ids:
            return []
        marks = ",".join("?" * len(chunk_ids))
        rows = self._conn().execute(
            f"SELECT * FROM chunks WHERE id IN ({marks})", chunk_ids
        ).fetchall()
        return [dict(r) for r in rows]

    def get_items(self, item_ids: list[int]) -> dict[int, dict]:
        if not item_ids:
            return {}
        marks = ",".join("?" * len(item_ids))
        rows = self._conn().execute(
            f"SELECT * FROM items WHERE id IN ({marks})", item_ids
        ).fetchall()
        return {r["id"]: dict(r) for r in rows}

    def get_item(self, item_id: int) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM items WHERE id=?", (item_id,)
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["chunks"] = self._rows(self._conn().execute(
            "SELECT * FROM chunks WHERE item_id=? ORDER BY seq, id", (item_id,)
        ))
        return item

    def update_item_media(self, item_id: int, content_text=None,
                          media_paths=None):
        """管线产物回写:汇总正文与媒体文件列表。"""
        conn = self._conn()
        sets, args = [], []
        if content_text is not None:
            sets.append("content_text=?")
            args.append(content_text)
        if media_paths is not None:
            sets.append("media_paths=?")
            args.append(json.dumps(media_paths, ensure_ascii=False))
        if sets:
            args.append(item_id)
            conn.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", args)
            conn.commit()

    # ── FTS 关键词检索(FTS5 不可用或短查询时降级 LIKE)──────────────
    @staticmethod
    def _fts_query(q: str) -> str:
        return '"' + q.replace('"', '""') + '"'

    def fts_search_chunks(self, query: str, limit: int = 20) -> list[int]:
        q = query.strip()
        if not q:
            return []
        conn = self._conn()
        if self.fts_enabled and len(q) >= 3:
            try:
                rows = conn.execute(
                    "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ?"
                    " LIMIT ?", (self._fts_query(q), limit),
                ).fetchall()
                return [r["chunk_id"] for r in rows]
            except sqlite3.OperationalError:
                pass
        rows = conn.execute(
            "SELECT id FROM chunks WHERE content LIKE ? LIMIT ?",
            (f"%{q}%", limit),
        ).fetchall()
        return [r["id"] for r in rows]

    # ── jobs 队列 ─────────────────────────────────────────────────────
    def enqueue(self, type_, payload: dict, dedup_key=None, priority=0) -> int:
        conn = self._conn()
        if dedup_key:
            row = conn.execute(
                "SELECT id, status FROM jobs WHERE dedup_key=?", (dedup_key,)
            ).fetchone()
            if row:
                return row["id"]
        cur = conn.execute(
            "INSERT INTO jobs(type, payload, dedup_key, priority) VALUES(?,?,?,?)",
            (type_, json.dumps(payload, ensure_ascii=False), dedup_key, priority),
        )
        conn.commit()
        return cur.lastrowid

    def claim_next(self) -> dict | None:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='pending'"
                " ORDER BY priority DESC, id LIMIT 1"
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE jobs SET status='running', started_at=datetime('now','localtime')"
                    " WHERE id=?", (row["id"],),
                )
            conn.commit()
            if not row:
                return None
            out = dict(row)
            out["status"] = "running"  # 返回更新后的状态,而非更新前快照
            return out
        except Exception:
            conn.rollback()
            raise

    def finish_job(self, job_id: int, ok: bool, error: str = None):
        conn = self._conn()
        conn.execute(
            "UPDATE jobs SET status=?, error=?, finished_at=datetime('now','localtime')"
            " WHERE id=?",
            ("done" if ok else "failed", error, job_id),
        )
        conn.commit()

    # ── 画像 / 日志 ───────────────────────────────────────────────────
    def upsert_profile(self, key, value, domain="general", source="manual"):
        conn = self._conn()
        row = conn.execute(
            "SELECT id FROM profile WHERE domain=? AND key=?", (domain, key)
        ).fetchone()
        vj = json.dumps(value, ensure_ascii=False)
        if row:
            conn.execute(
                "UPDATE profile SET value_json=?, source=?, updated_at=datetime('now','localtime')"
                " WHERE id=?", (vj, source, row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO profile(domain, key, value_json, source) VALUES(?,?,?,?)",
                (domain, key, vj, source),
            )
        conn.commit()

    def get_profile(self, domain=None) -> list[dict]:
        sql = "SELECT * FROM profile"
        args = ()
        if domain:
            sql += " WHERE domain=?"
            args = (domain,)
        return self._rows(self._conn().execute(sql + " ORDER BY domain, key", args))

    def add_log(self, content, domain=None, happened_at=None, attrs=None,
                related_item_ids=None, source="chat"):
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO logs(domain, happened_at, content_text, attrs_json,"
            " related_item_ids, source) VALUES(?,?,?,?,?,?)",
            (domain,
             happened_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             content, json.dumps(attrs or {}, ensure_ascii=False),
             json.dumps(related_item_ids or [], ensure_ascii=False), source),
        )
        conn.commit()
        return cur.lastrowid

    def timeline(self, days=7, domain=None) -> list[dict]:
        sql = ("SELECT * FROM logs WHERE happened_at >="
               " datetime('now', 'localtime', ?)")
        args = [f"-{int(days)} days"]
        if domain:
            sql += " AND domain=?"
            args.append(domain)
        sql += " ORDER BY happened_at DESC"
        return self._rows(self._conn().execute(sql, args))

    # ── 分类 ──────────────────────────────────────────────────────────
    def add_category(self, domain, name, parent_id=None, sort=0,
                     status="active") -> int:
        cur = self._conn().execute(
            "INSERT INTO categories(domain, parent_id, name, sort, status)"
            " VALUES(?,?,?,?,?)",
            (domain, parent_id, name, sort, status),
        )
        self._conn().commit()
        return cur.lastrowid

    def confirm_category(self, category_id: int):
        conn = self._conn()
        conn.execute("UPDATE categories SET status='active' WHERE id=?",
                     (category_id,))
        conn.commit()

    def get_category(self, category_id: int) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM categories WHERE id=?", (category_id,)
        ).fetchone()
        return dict(row) if row else None

    def set_item_category(self, item_id: int, category_id: int | None,
                          conf: float | None = None, note: str | None = None):
        conn = self._conn()
        conn.execute(
            "UPDATE items SET category_id=?, category_conf=?, auto_note=? WHERE id=?",
            (category_id, conf, note, item_id),
        )
        conn.commit()

    # ── 订阅源(M3b)──────────────────────────────────────────────────
    def add_watch_source(self, kind, target, domain=None, enabled=True,
                         label=None) -> int:
        cur = self._conn().execute(
            "INSERT INTO watch_sources(kind, target, domain, enabled, label)"
            " VALUES(?,?,?,?,?)", (kind, target, domain, int(enabled), label),
        )
        self._conn().commit()
        return cur.lastrowid

    def get_watch_source(self, source_id: int) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM watch_sources WHERE id=?", (source_id,)
        ).fetchone()
        return dict(row) if row else None

    def watch_sources(self, enabled_only=False) -> list[dict]:
        sql = "SELECT * FROM watch_sources"
        if enabled_only:
            sql += " WHERE enabled=1"
        return self._rows(self._conn().execute(sql + " ORDER BY id"))

    def toggle_watch_source(self, source_id: int):
        conn = self._conn()
        conn.execute("UPDATE watch_sources SET enabled=1-enabled WHERE id=?",
                     (source_id,))
        conn.commit()

    def touch_watch_source(self, source_id: int):
        from datetime import datetime

        conn = self._conn()
        conn.execute(
            "UPDATE watch_sources SET last_checked=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), source_id),
        )
        conn.commit()

    def job_exists(self, dedup_key: str) -> bool:
        return self._conn().execute(
            "SELECT 1 FROM jobs WHERE dedup_key=?", (dedup_key,)
        ).fetchone() is not None

    def categories(self, domain=None) -> list[dict]:
        sql = "SELECT * FROM categories"
        args = ()
        if domain:
            sql += " WHERE domain=?"
            args = (domain,)
        return self._rows(self._conn().execute(sql + " ORDER BY sort, id", args))

    # ── 提示词(M3 启用,M1 先建表与读写)────────────────────────────
    def add_prompt(self, name, stage, content, category_id=None,
                   status="active", origin="system") -> int:
        cur = self._conn().execute(
            "INSERT INTO prompts(name, stage, category_id, content, status, origin)"
            " VALUES(?,?,?,?,?,?)",
            (name, stage, category_id, content, status, origin),
        )
        self._conn().commit()
        return cur.lastrowid

    def active_prompt(self, name, stage, category_id=None) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM prompts WHERE name=? AND stage=? AND category_id IS ?"
            " AND status='active' ORDER BY version DESC LIMIT 1",
            (name, stage, category_id),
        ).fetchone()
        return dict(row) if row else None

    # ── 面板查询 ──────────────────────────────────────────────────────
    def list_items(self, status=None, domain=None, limit=50, offset=0) -> list[dict]:
        sql = ("SELECT i.*, (SELECT COUNT(*) FROM chunks c WHERE c.item_id = i.id)"
               " AS chunk_count FROM items i")
        where, args = [], []
        if status:
            where.append("i.status=?"); args.append(status)
        if domain:
            where.append("i.domain=?"); args.append(domain)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY i.id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        return self._rows(self._conn().execute(sql, args))

    def count_items(self, status=None, domain=None, category_id=None) -> int:
        sql = "SELECT COUNT(*) c FROM items"
        where, args = [], []
        if status:
            where.append("status=?"); args.append(status)
        if domain:
            where.append("domain=?"); args.append(domain)
        if category_id:
            where.append("category_id=?"); args.append(category_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self._conn().execute(sql, args).fetchone()["c"]

    def items_by_domain(self, status=None) -> list[dict]:
        sql = ("SELECT domain, COUNT(*) c FROM items")
        args = []
        if status:
            sql += " WHERE status=?"; args.append(status)
        sql += " GROUP BY domain ORDER BY c DESC"
        return self._rows(self._conn().execute(sql, args))

    def set_item_status(self, item_id: int, status: str):
        conn = self._conn()
        conn.execute("UPDATE items SET status=? WHERE id=?", (status, item_id))
        conn.commit()

    def bulk_set_status(self, status_from: str, status_to: str,
                        domain: str = None) -> int:
        conn = self._conn()
        sql, args = "UPDATE items SET status=? WHERE status=?", [status_to, status_from]
        if domain:
            sql += " AND domain=?"; args.append(domain)
        n = conn.execute(sql, args).rowcount
        conn.commit()
        return n

    def list_jobs(self, limit=50) -> list[dict]:
        return self._rows(self._conn().execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ))

    # ── 统计 ──────────────────────────────────────────────────────────
    def stats(self) -> dict:
        conn = self._conn()
        out = {}
        for t in ("items", "chunks", "logs", "jobs", "categories"):
            out[t] = conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        out["inbox"] = conn.execute(
            "SELECT COUNT(*) c FROM items WHERE status='inbox'"
        ).fetchone()["c"]
        return out
