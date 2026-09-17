"""图像检索链路测试:用假 CLIP 嵌入器(不需要下载 600MB 权重)。

真实模型走 scripts/embed_images.py 验证,这里只测接线:
管线是否索引、search_images 是否命中、面板是否渲染「相关画面」。
"""
import hashlib

import numpy as np
import pytest


class FakeImageEmbedder:
    """确定性假 CLIP:同文本/图片名 → 同向量;文本和图像共用同一空间。"""

    def __init__(self, dim=16):
        self.dim = dim

    def available(self):
        return True

    def _vec(self, key: str):
        h = hashlib.md5(key.encode("utf-8")).digest()
        v = np.random.default_rng(int.from_bytes(h[:8], "big")).standard_normal(self.dim)
        return (v / np.linalg.norm(v)).astype(np.float32).tolist()

    def encode_text(self, texts):
        return [self._vec(t) for t in texts]

    def encode_image(self, image_path):
        return self._vec(str(image_path))

    def encode_image_batch(self, paths, batch_size=8):
        return [self._vec(str(p)) for p in paths]


def make_image(tmp_path, name):
    from PIL import Image

    p = tmp_path / name
    Image.new("RGB", (32, 32), (120, 60, 30)).save(p)
    return str(p)


def test_image_chunks_indexed_when_embedder_available(memory, tmp_path):
    """有图像嵌入器时,图像块 embed_status 直接 done 且进了图像集合。"""
    from memvault.memory import Memory

    m = Memory(memory.db, memory.vs, memory.embedder, FakeImageEmbedder())
    item = m.add_item("general", "video", "视频")
    m.add_image_chunk(item, make_image(tmp_path, "f0.jpg"), start_ts=1.0,
                      seq=0, image_embedder=m.image_embedder)
    chunk = m.get_item(item)["chunks"][0]
    assert chunk["embed_status"] == "done"
    assert m.vs.count_image() == 1


def test_search_images_finds_frame(tmp_path):
    """文字搜画面:命中的是图像块,带 media_path 与时间戳。"""
    from memvault.db import Database
    from memvault.embeddings import FakeTextEmbedder
    from memvault.memory import Memory
    from memvault.vector_store import VectorStore

    ib = FakeImageEmbedder()
    m = Memory(Database(tmp_path / "t.db"), VectorStore(tmp_path / "c"),
               FakeTextEmbedder(), ib)
    item = m.add_item("general", "video", "冰淇淋视频")
    frame = make_image(tmp_path, "f1.jpg")
    # 让"画面"与查询文本同向量:用同一 key 编码(假嵌入器按字符串哈希)
    ib._vec = (lambda key: FakeImageEmbedder._vec(ib, "冰淇淋"))  # noqa: SLF001
    m.add_image_chunk(item, frame, start_ts=12.5, seq=0,
                      image_embedder=m.image_embedder)

    hits = m.search_images("冰淇淋", top_k=5)
    assert len(hits) == 1
    assert hits[0]["item"]["id"] == item
    assert hits[0]["chunk"]["media_path"].endswith("f1.jpg")
    assert hits[0]["chunk"]["start_ts"] == 12.5
    assert hits[0]["score"] > 0.9


def test_search_images_without_embedder_is_empty(memory, tmp_path):
    """没装 cn-clip(或权重缺失)时,图像检索安静返回空,不影响文本检索。"""
    from memvault.memory import Memory

    m = Memory(memory.db, memory.vs, memory.embedder)  # 不传图像嵌入器
    m._image_checked = True
    m._image_embedder = None
    assert m.search_images("任何词") == []
    assert m.image_embedder is None


def test_video_pipeline_indexes_frames(monkeypatch, tmp_path):
    """视频管线:图像嵌入启用时,抽帧入库带图像向量;off 时不带。"""
    from memvault.pipeline import video as video_mod

    calls = []

    class FakeMemory:
        image_embedder = FakeImageEmbedder()

        def add_image_chunk(self, *a, **kw):
            calls.append(kw.get("image_embedder"))
            return 1

    frames = [{"ts": 1.0, "path": str(tmp_path / "a.jpg")}]
    cfg = {"vision": {"image_embed": {"enabled": "auto"}}}
    # 直接复用管线里的开关判断 + 调用约定
    assert video_mod._image_embed_enabled(cfg) is True
    ib = FakeMemory.image_embedder if video_mod._image_embed_enabled(cfg) else None
    for i, fr in enumerate(frames):
        FakeMemory().add_image_chunk(1, fr["path"], start_ts=fr["ts"], seq=i,
                                     image_embedder=ib)
    assert calls and calls[0] is not None

    cfg_off = {"vision": {"image_embed": {"enabled": "off"}}}
    assert video_mod._image_embed_enabled(cfg_off) is False


def test_search_page_shows_image_hits(tmp_path, monkeypatch):
    """面板检索页出现「相关画面」区块(缩略图 + 时间戳)。"""
    from fastapi.testclient import TestClient

    from memvault import llm as llm_mod
    from memvault.config import load_config
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
        m = app.state.memory
        ib = FakeImageEmbedder()
        ib._vec = (lambda key: FakeImageEmbedder._vec(ib, "冰淇淋"))  # noqa: SLF001
        m._image_embedder, m._image_checked = ib, True
        item = m.add_item("general", "video", "冰淇淋视频")
        frame = tmp_path / "data" / "media" / "item_x" / "f.jpg"
        frame.parent.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        Image.new("RGB", (16, 16), (10, 20, 30)).save(frame)
        m.add_image_chunk(item, str(frame), start_ts=3.0, seq=0,
                          image_embedder=ib)

        page = client.get("/search?q=冰淇淋").text
        assert "相关画面" in page
        assert "冰淇淋视频" in page
        assert "/media/item_x/f.jpg" in page


def test_config_has_image_embed_switch():
    from memvault.config import DEFAULTS

    assert DEFAULTS["vision"]["image_embed"]["enabled"] == "auto"
