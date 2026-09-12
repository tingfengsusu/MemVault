"""嵌入模型封装。

- 文本:bge-small-zh-v1.5(sentence-transformers);国内网络自动走 hf-mirror。
- fake 模式:确定性假嵌入(同一文本同一向量),供离线测试。
- 图像:Chinese-CLIP 懒加载,未安装 requirements-vision 时不可用。
"""
import hashlib
import logging
import os

# 国内镜像,必须在 import transformers/sentence_transformers 之前设置
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

import numpy as np

logger = logging.getLogger(__name__)

FAKE_DIM = 512


class FakeTextEmbedder:
    """确定性假嵌入:同文本同向量,不同文本近似正交。仅供测试/离线。"""

    dim = FAKE_DIM

    def encode(self, texts):
        out = []
        for t in texts:
            h = hashlib.md5(t.encode("utf-8")).digest()
            rng = np.random.default_rng(int.from_bytes(h[:8], "big"))
            v = rng.standard_normal(FAKE_DIM).astype(np.float32)
            out.append((v / np.linalg.norm(v)).tolist())
        return out


class STTextEmbedder:
    """sentence-transformers 文本嵌入,模型懒加载。"""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None

    @property
    def dim(self):
        self._ensure()
        return self._model.get_sentence_embedding_dimension()

    def _ensure(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.info("加载文本嵌入模型 %s ...", self.model_name)
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def encode(self, texts):
        model = self._ensure()
        vecs = model.encode(texts, normalize_embeddings=True)
        return [v.tolist() for v in np.asarray(vecs, dtype=np.float32)]


_text_embedder = None


def get_text_embedder(cfg: dict):
    """按配置返回文本嵌入器(单例)。模型加载失败时自动降级 fake。"""
    global _text_embedder
    if _text_embedder is not None:
        return _text_embedder
    ecfg = cfg.get("embedding", {})
    if ecfg.get("fake"):
        _text_embedder = FakeTextEmbedder()
        return _text_embedder
    try:
        _text_embedder = STTextEmbedder(ecfg.get("text_model", "BAAI/bge-small-zh-v1.5"))
        _text_embedder.encode([" warmup "])  # 触发加载,尽早暴露网络/模型问题
    except Exception as e:  # noqa: BLE001 — 任何加载失败都降级,保证管线可用
        logger.warning("文本嵌入模型加载失败(%s),降级为 fake 嵌入", e)
        _text_embedder = FakeTextEmbedder()
    return _text_embedder


class ImageEmbedder:
    """Chinese-CLIP 图像嵌入(M1 可选)。"""

    def __init__(self, model_name="OFA-Sys/chinese-clip-vit-base-p16"):
        self.model_name = model_name
        self._model = None
        self._processor = None

    def available(self) -> bool:
        try:
            import cn_clip  # noqa: F401
            return True
        except ImportError:
            return False

    def _ensure(self):
        if self._model is None:
            from cn_clip.clip import load_from_name
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
            self._model, self._processor = load_from_name(
                self.model_name.split("/")[-1], device=device, download_root=None
            )
            self._model.eval()
        return self._model, self._processor

    def encode_image(self, image_path: str):
        from PIL import Image
        import torch

        model, processor = self._ensure()
        image = Image.open(image_path).convert("RGB")
        with torch.no_grad():
            feats = model.encode_image(processor(image).unsqueeze(0).to(model.device))
            feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats[0].cpu().tolist()
