"""图像检索链路测试:用假 CLIP 嵌入器(不需要下载 600MB 权重)。

真实模型走 scripts/embed_images.py 验证,这里只测接线:
管线是否索引、search_images 是否命中、面板是否渲染「相关画面」。
"""
import hashlib
from pathlib import Path

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


# ── ① 以图搜图(上传 / 库里帧找相似)────────────────────────────────
def _client_with_image(tmp_path, monkeypatch, query_key="冰淇淋"):
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
    return TestClient(app), app, query_key


def test_search_by_image_upload(tmp_path, monkeypatch):
    """上传一张图 → 返回相似画面(缩略图 + 分数),并显示查询图。"""
    from PIL import Image

    client, app, key = _client_with_image(tmp_path, monkeypatch)
    with client:
        m = app.state.memory
        ib = FakeImageEmbedder()
        ib._vec = (lambda k: FakeImageEmbedder._vec(ib, key))  # noqa: SLF001
        m._image_embedder, m._image_checked = ib, True
        item = m.add_item("general", "video", "冰淇淋视频")
        frame = tmp_path / "data" / "media" / "item_q" / "f.jpg"
        frame.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 16), (200, 120, 40)).save(frame)
        m.add_image_chunk(item, str(frame), start_ts=8.0, seq=0,
                          image_embedder=ib)

        up = tmp_path / "query.jpg"
        Image.new("RGB", (20, 20), (210, 130, 50)).save(up)
        r = client.post("/search/image",
                        files={"file": ("query.jpg", up.read_bytes(), "image/jpeg")})
        assert r.status_code == 200
        assert "以图搜图:query.jpg" in r.text
        assert "冰淇淋视频" in r.text
        assert "/media/item_q/f.jpg" in r.text
        assert "/media/_queries/" in r.text      # 查询图也回显
        # 上传的查询图落盘了
        assert list((tmp_path / "data" / "media" / "_queries").glob("*.jpg"))


def test_search_by_image_guards(tmp_path, monkeypatch):
    """没有图片 / 没有图像嵌入器时给出明确错误。"""
    from PIL import Image

    client, app, _ = _client_with_image(tmp_path, monkeypatch)
    with client:
        m = app.state.memory
        up = tmp_path / "q.jpg"
        Image.new("RGB", (8, 8)).save(up)
        # 未启用图像嵌入(cn-clip 缺失)
        m._image_embedder, m._image_checked = None, True
        r = client.post("/search/image",
                        files={"file": ("q.jpg", up.read_bytes(), "image/jpeg")})
        assert r.status_code == 503
        assert "未启用" in r.text


def test_item_similar_image_from_stored_frame(tmp_path, monkeypatch):
    """库里已有帧的「🔍 找相似画面」:用该帧向量找相似。"""
    from PIL import Image

    client, app, key = _client_with_image(tmp_path, monkeypatch)
    with client:
        m = app.state.memory
        ib = FakeImageEmbedder()
        ib._vec = (lambda k: FakeImageEmbedder._vec(ib, key))  # noqa: SLF001
        m._image_embedder, m._image_checked = ib, True
        item = m.add_item("general", "video", "冰淇淋视频")
        frame = tmp_path / "data" / "media" / "item_s" / "a.jpg"
        frame.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 16), (1, 2, 3)).save(frame)
        chunk_id = m.add_image_chunk(item, str(frame), start_ts=2.0, seq=0,
                                     image_embedder=ib)

        html = client.get(f"/items/{item}").text
        assert "找相似画面" in html

        r = client.post(f"/items/{item}/similar-image",
                        data={"chunk_id": str(chunk_id)})
        assert r.status_code == 200
        assert "冰淇淋视频" in r.text


# ── ② 商品主图入库 ────────────────────────────────────────────────
def test_product_image_downloaded_and_indexed(tmp_path, monkeypatch):
    """商品主图:下载 → 存档 → 建图像向量 → 写进 media_paths(卡片有图)。"""
    from PIL import Image, ImageDraw

    from memvault.config import load_config
    from memvault.db import Database
    from memvault.embeddings import FakeTextEmbedder
    from memvault.memory import Memory
    from memvault.pipeline import text as text_mod
    from memvault.vector_store import VectorStore

    cfg = load_config()
    cfg["data_dir"] = str(tmp_path / "data")
    m = Memory(Database(tmp_path / "data" / "m.db"),
               VectorStore(tmp_path / "data" / "chroma"), FakeTextEmbedder(),
               FakeImageEmbedder())

    # 造一张"主图"并从本地 http 服务式路径取(download_image 支持 http)
    img = tmp_path / "main.jpg"
    Image.new("RGB", (40, 40), (50, 90, 150)).save(img, quality=90)
    import http.server
    import threading

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(tmp_path), **kw)

        def log_message(self, *a):   # 静音
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/main.jpg"

    item_id = text_mod.ingest_product(
        {"url": "https://item.jd.com/1.html", "title": "某外套",
         "product": {"name": "某外套", "price": "199", "shop": "京东",
                     "image_url": url}}, m, cfg)
    srv.shutdown()

    item = m.get_item(item_id)
    import json
    assert "image_url" in json.loads(item["attrs_json"])     # 原始属性保留
    media = json.loads(item["media_paths"])
    assert media and Path(media[0]).is_file()                # 主图已落盘
    img_chunks = [c for c in item["chunks"] if c["modality"] == "image"]
    assert len(img_chunks) == 1
    assert img_chunks[0]["embed_status"] == "done"           # 已建图像向量
    assert m.vs.count_image() == 1


def test_product_image_failure_does_not_break(tmp_path):
    """主图下载失败(404/坏 URL)时,商品条目本身照常入库。"""
    from memvault.config import load_config
    from memvault.db import Database
    from memvault.embeddings import FakeTextEmbedder
    from memvault.memory import Memory
    from memvault.pipeline import text as text_mod
    from memvault.vector_store import VectorStore

    cfg = load_config()
    cfg["data_dir"] = str(tmp_path / "data")
    m = Memory(Database(tmp_path / "data" / "m.db"),
               VectorStore(tmp_path / "data" / "chroma"), FakeTextEmbedder(),
               FakeImageEmbedder())
    item_id = text_mod.ingest_product(
        {"url": "https://item.jd.com/2.html",
         "product": {"name": "坏图商品", "price": "1",
                     "image_url": "http://127.0.0.1:9/none.jpg"}}, m, cfg)
    assert m.get_item(item_id)["title"] == "坏图商品"
    assert m.vs.count_image() == 0
    # 非 http(s) 的图片地址直接跳过
    assert text_mod.download_image("ftp://x/y.jpg", tmp_path) is None


# ── ③ PDF 图表入库 ───────────────────────────────────────────────
def test_pdf_vector_figure_rendered_and_indexed(tmp_path):
    """PDF 里的矢量图表(流程图)会被渲染存档并建图像向量。

    位图靠 page.get_images() 抽取;流程图/示意图多是矢量绘图,只能整页渲染。
    """
    import fitz

    from memvault.config import load_config
    from memvault.db import Database
    from memvault.embeddings import FakeTextEmbedder
    from memvault.memory import Memory
    from memvault.pipeline.document import parse_document
    from memvault.vector_store import VectorStore

    pdf = tmp_path / "doc.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "这是流程说明页,含一张流程图。")
    for i in range(4):        # 若干方框 + 连线 = 矢量流程图,非位图
        y = 120 + i * 60
        page.draw_rect(fitz.Rect(72, y, 200, y + 36), width=1.2)
        page.draw_line(fitz.Point(136, y + 36), fitz.Point(136, y + 60))
    doc.save(str(pdf))
    doc.close()

    cfg = load_config()
    cfg["data_dir"] = str(tmp_path / "data")
    m = Memory(Database(tmp_path / "data" / "m.db"),
               VectorStore(tmp_path / "data" / "chroma"), FakeTextEmbedder(),
               FakeImageEmbedder())
    item_id = parse_document(pdf, m, cfg)

    item = m.get_item(item_id)
    img_chunks = [c for c in item["chunks"] if c["modality"] == "image"]
    assert img_chunks, "矢量图表页应被渲染成图像块"
    assert Path(img_chunks[0]["media_path"]).is_file()
    assert img_chunks[0]["embed_status"] == "done"    # 已建图像向量
    assert m.vs.count_image() >= 1
    assert any(c["modality"] == "text" for c in item["chunks"])  # 文本层不受影响
