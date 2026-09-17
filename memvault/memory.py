"""记忆 API — 对上层(技能/面板)的唯一检索与写入入口。

设计见 DESIGN.md §6。search 为混合召回:
向量(Chroma) + 关键词(SQLite FTS5) → RRF 融合。
"""
import json
import logging

from memvault.db import Database
from memvault.embeddings import get_text_embedder
from memvault.vector_store import VectorStore

logger = logging.getLogger(__name__)

RRF_K = 60  # RRF 常数,标准取值


class Memory:
    def __init__(self, db: Database, vs: VectorStore, embedder=None,
                 image_embedder=None):
        self.db = db
        self.vs = vs
        self._embedder = embedder
        self._image_embedder = image_embedder
        self._image_checked = image_embedder is not None

    @property
    def embedder(self):
        if self._embedder is None:
            raise RuntimeError("Memory 未配置嵌入器(Memory(db, vs, embedder))")
        return self._embedder

    @property
    def image_embedder(self):
        """Chinese-CLIP 图像嵌入器;未安装/权重缺失时为 None(自动跳过图像侧)。"""
        if not self._image_checked:
            self._image_checked = True
            from memvault.embeddings import ImageEmbedder

            ib = ImageEmbedder()
            self._image_embedder = ib if ib.available() else None
        return self._image_embedder

    # ── 写入 ──────────────────────────────────────────────────────────
    def add_item(self, domain, type_, title, **kw) -> int:
        return self.db.add_item(domain, type_, title, **kw)

    def add_text_chunk(self, item_id: int, content: str, start_ts=None,
                       end_ts=None, seq=None, extra_meta=None) -> int:
        """文本块:入库 → 嵌入 → 进向量索引。"""
        if not content or not content.strip():
            return 0
        chunk_id = self.db.add_chunk(
            item_id, "text", content=content, start_ts=start_ts,
            end_ts=end_ts, seq=seq,
        )
        item = self.db.get_items([item_id])[item_id]
        meta = {
            "item_id": item_id,
            "domain": item["domain"],
            "type": item["type"],
            "category_id": item["category_id"],
            "source_type": item["source_type"],
            "start_ts": start_ts,
        }
        # Chroma 拒绝 None 元数据值
        meta = {k: v for k, v in meta.items() if v is not None}
        meta.update(extra_meta or {})
        vecs = self.embedder.encode([content])
        self.vs.upsert_text([chunk_id], vecs, [content], [meta])
        self.db._conn().execute(
            "UPDATE chunks SET embed_status='done' WHERE id=?", (chunk_id,)
        )
        self.db._conn().commit()
        return chunk_id

    def add_image_chunk(self, item_id: int, media_path: str, start_ts=None,
                        seq=None, image_embedder=None) -> int:
        """图像块:入库;有 CLIP 则索引,否则保持 pending(M1 允许)。"""
        chunk_id = self.db.add_chunk(
            item_id, "image", media_path=media_path, start_ts=start_ts, seq=seq,
        )
        item = self.db.get_items([item_id])[item_id]
        meta = {
            "item_id": item_id,
            "domain": item["domain"],
            "start_ts": start_ts,
            "media_path": str(media_path),
        }
        meta = {k: v for k, v in meta.items() if v is not None}
        if image_embedder is not None and image_embedder.available():
            vec = image_embedder.encode_image(str(media_path))
            self.vs.upsert_image([chunk_id], [vec], [meta])
            self.db._conn().execute(
                "UPDATE chunks SET embed_status='done' WHERE id=?", (chunk_id,)
            )
            self.db._conn().commit()
        return chunk_id

    def add_text_chunks_batch(self, item_id: int, chunks: list[dict]) -> int:
        """批量写入文本块:一次嵌入调用 + 一次向量库 upsert(比逐条快数倍)。

        chunks: [{"content", "start_ts"?, "end_ts"?, "seq"?}, ...]
        """
        chunks = [c for c in chunks if (c.get("content") or "").strip()]
        if not chunks:
            return 0
        item = self.db.get_items([item_id])[item_id]
        base_meta = {
            "item_id": item_id, "domain": item["domain"], "type": item["type"],
            "category_id": item["category_id"],
            "source_type": item["source_type"],
        }
        base_meta = {k: v for k, v in base_meta.items() if v is not None}

        ids, metas, docs = [], [], []
        for c in chunks:
            cid = self.db.add_chunk(
                item_id, "text", content=c["content"],
                start_ts=c.get("start_ts"), end_ts=c.get("end_ts"),
                seq=c.get("seq"))
            meta = dict(base_meta)
            if c.get("start_ts") is not None:
                meta["start_ts"] = c["start_ts"]
            ids.append(cid)
            metas.append(meta)
            docs.append(c["content"])

        vecs = self.embedder.encode(docs)  # 单次批量编码
        self.vs.upsert_text(ids, vecs, docs, metas)
        self.db._conn().executemany(
            "UPDATE chunks SET embed_status='done' WHERE id=?",
            [(i,) for i in ids])
        self.db._conn().commit()
        return len(ids)

    # ── 检索 ──────────────────────────────────────────────────────────
    @staticmethod
    def _build_where(domain=None, type_=None, category_id=None):
        where = {}
        if domain:
            where["domain"] = domain
        if type_:
            where["type"] = type_
        if category_id:
            where["category_id"] = category_id
        return where or None

    def search(self, query: str, domain=None, type_=None, category_id=None,
               top_k=8) -> list[dict]:
        """混合检索,返回 [{score, chunk, item}],score 为 RRF 融合分。"""
        if not query.strip():
            return []
        vec = self.embedder.encode([query])[0]

        # 1) 向量召回
        vec_hits = []
        try:
            vec_hits = self.vs.query_text(
                vec, where=self._build_where(domain, type_, category_id),
                k=top_k * 2,
            )
        except Exception as e:  # noqa: BLE001 — 向量库故障时退化为纯关键词
            logger.warning("向量检索失败:%s", e)

        # 2) 关键词召回(FTS5/LIKE,域过滤在融合阶段做)
        kw_ids = self.db.fts_search_chunks(query, limit=top_k * 2)

        # 3) RRF 融合
        scores: dict[int, float] = {}
        for rank, hit in enumerate(vec_hits):
            scores[hit["chunk_id"]] = scores.get(hit["chunk_id"], 0.0) + 1.0 / (RRF_K + rank + 1)
        for rank, cid in enumerate(kw_ids):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)

        chunk_ids = sorted(scores, key=lambda c: scores[c], reverse=True)
        chunks = {c["id"]: c for c in self.db.get_chunks(chunk_ids)}

        results = []
        for cid in chunk_ids[: top_k * 2]:
            chunk = chunks.get(cid)
            if chunk is None:
                continue
            item = self.db.get_items([chunk["item_id"]]).get(chunk["item_id"])
            if item is None:
                continue
            if domain and item["domain"] != domain:
                continue
            if type_ and item["type"] != type_:
                continue
            if category_id and item["category_id"] != category_id:
                continue
            results.append({
                "score": round(scores[cid], 6),
                "chunk": chunk,
                "item": item,
            })
            if len(results) >= top_k:
                break
        return results

    # ── 图像检索(Chinese-CLIP;文字搜画面 / 以图搜图)──────────────────
    def search_images(self, query: str, domain=None, top_k=6,
                      image_embedder=None) -> list[dict]:
        """用文字搜画面,返回 [{score, chunk, item}]。

        图像块没有文本可比对,只能走向量:CLIP 把文字与画面编码到同一空间,
        因此"冰淇淋"能检索到画面里有冰淇淋的帧。需要 cn_clip + 权重已就绪。
        """
        if not (query or "").strip():
            return []
        ib = image_embedder or self.image_embedder
        if ib is None or not ib.available():
            return []
        try:
            vec = ib.encode_text([query])[0]
            hits = self.vs.query_image(
                vec, where=self._build_where(domain, None, None), k=top_k * 2)
        except Exception as e:  # noqa: BLE001 — 图像检索失败不影响文本检索
            logger.warning("图像检索失败:%s", e)
            return []
        chunks = {c["id"]: c for c in self.db.get_chunks(
            [h["chunk_id"] for h in hits])}
        results = []
        for h in hits:
            chunk = chunks.get(h["chunk_id"])
            if chunk is None:
                continue
            item = self.db.get_items([chunk["item_id"]]).get(chunk["item_id"])
            if item is None or (domain and item["domain"] != domain):
                continue
            results.append({
                "score": round(1.0 - float(h.get("distance", 1.0)), 4),
                "chunk": chunk,
                "item": item,
            })
            if len(results) >= top_k:
                break
        return results

    # ── 画像 / 日志 ───────────────────────────────────────────────────
    def set_profile(self, key, value, domain="general", source="manual"):
        self.db.upsert_profile(key, value, domain=domain, source=source)

    def get_profile(self, domain=None) -> list[dict]:
        return self.db.get_profile(domain)

    def add_log(self, content, domain=None, happened_at=None, attrs=None,
                related_item_ids=None, source="chat") -> int:
        return self.db.add_log(content, domain=domain, happened_at=happened_at,
                               attrs=attrs, related_item_ids=related_item_ids,
                               source=source)

    def timeline(self, days=7, domain=None) -> list[dict]:
        return self.db.timeline(days=days, domain=domain)

    # ── 其他 ──────────────────────────────────────────────────────────
    def get_item(self, item_id: int) -> dict | None:
        return self.db.get_item(item_id)

    def reindex_text(self, batch_size: int = 64, progress=None) -> int:
        """用当前嵌入模型重建全部文本块向量(换模型/修复降级后用)。"""
        rows = self.db._rows(self.db._conn().execute(
            "SELECT id, item_id, content FROM chunks"
            " WHERE modality='text' AND content IS NOT NULL"
        ))
        done = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            items = self.db.get_items(list({b["item_id"] for b in batch}))
            ids, metas, docs = [], [], []
            for b in batch:
                item = items[b["item_id"]]
                meta = {"item_id": b["item_id"], "domain": item["domain"],
                        "type": item["type"],
                        "category_id": item["category_id"],
                        "source_type": item["source_type"],
                        "start_ts": None}
                meta = {k: v for k, v in meta.items() if v is not None}
                ids.append(b["id"])
                metas.append(meta)
                docs.append(b["content"])
            vecs = self.embedder.encode(docs)
            self.vs.upsert_text(ids, vecs, docs, metas)
            self.db._conn().executemany(
                "UPDATE chunks SET embed_status='done' WHERE id=?",
                [(cid,) for cid in ids],
            )
            self.db._conn().commit()
            done += len(batch)
            if progress:
                progress(f"reindex {done}/{len(rows)}")
        return done

    def stats(self) -> dict:
        return self.db.stats()


def fmt_ts(seconds: float | None) -> str:
    """秒 → mm:ss,溯源跳转展示用。"""
    if seconds is None:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"
