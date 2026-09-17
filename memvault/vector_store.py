"""向量库封装(Chroma)。

两个 collection:text(bge 文本向量)与 image(CLIP 图像向量,M1 可选)。
检索时通过 metadata 过滤 domain/type/category。
"""
import logging

import chromadb

logger = logging.getLogger(__name__)


class VectorStore:
    def __init__(self, path):
        self.client = chromadb.PersistentClient(path=str(path))
        self.text = self.client.get_or_create_collection(
            "chunk_text", metadata={"hnsw:space": "cosine"}
        )
        self._image = None

    @property
    def image(self):
        """图像 collection 懒创建(装了 CLIP 才用)。"""
        if self._image is None:
            self._image = self.client.get_or_create_collection(
                "chunk_image", metadata={"hnsw:space": "cosine"}
            )
        return self._image

    def upsert_text(self, chunk_ids, vectors, documents, metadatas):
        self.text.upsert(
            ids=[str(i) for i in chunk_ids],
            embeddings=vectors,
            documents=documents,
            metadatas=metadatas,
        )

    def upsert_image(self, chunk_ids, vectors, metadatas):
        self.image.upsert(
            ids=[str(i) for i in chunk_ids],
            embeddings=vectors,
            metadatas=metadatas,
        )

    def query_text(self, vector, where=None, k=10) -> list[dict]:
        res = self.text.query(
            query_embeddings=[vector],
            n_results=min(k, max(1, self.text.count())),
            where=where or None,
        )
        ids = res.get("ids", [[]])[0]
        distances = res.get("distances", [[]])[0]
        metadatas = res.get("metadatas", [[]])[0]
        return [
            {"chunk_id": int(cid), "distance": d, "metadata": m or {}}
            for cid, d, m in zip(ids, distances, metadatas)
        ]

    def delete_by_item_ids(self, item_ids: list[int]):
        """删除这些条目在文本/图像向量库中的全部向量(条目重采集时清理)。"""
        if not item_ids:
            return
        where = {"item_id": {"$in": list(item_ids)}}
        for coll in (self.text, self.image if self._image else None):
            if coll is None:
                continue
            try:
                coll.delete(where=where)
            except Exception as e:  # noqa: BLE001 — 空集合等场景容错
                logger.warning("向量删除失败(%s): %s", coll.name, e)

    def count_text(self) -> int:
        return self.text.count()

    # ── 质疑记忆库(提示词进化的历史教训)─────────────────────────────
    @property
    def feedback(self):
        if not hasattr(self, "_feedback") or self._feedback is None:
            self._feedback = self.client.get_or_create_collection(
                "feedback", metadata={"hnsw:space": "cosine"}
            )
        return self._feedback

    def upsert_feedback(self, ids, vectors, documents, metadatas):
        self.feedback.upsert(
            ids=[str(i) for i in ids], embeddings=vectors,
            documents=documents, metadatas=metadatas,
        )

    def query_feedback(self, vector, where=None, k=5) -> list[int]:
        total = self.feedback.count()
        if total == 0:
            return []
        res = self.feedback.query(
            query_embeddings=[vector], n_results=min(k, total),
            where=where or None,
        )
        return [int(i) for i in res.get("ids", [[]])[0]]
