"""嵌入模型封装。

- 文本:bge-small-zh-v1.5(sentence-transformers);国内网络自动走 hf-mirror。
- fake 模式:确定性假嵌入(同一文本同一向量),供离线测试。
- 图像:Chinese-CLIP 懒加载,未安装 requirements-vision 时不可用。
"""
import hashlib
import logging
import os

# 国内镜像,必须在 import transformers/sentence_transformers 之前设置;
# HF_HUB_DISABLE_XET:Xet 存储的 CAS 接口镜像无法代理(401)
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

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
    """Chinese-CLIP 图像/文本双编码器(可选)。

    文本与图像编码到同一空间:既能用文字搜画面,也能用一张图找相似画面。
    权重经 cn_clip 下载到 `~/.cache/clip`(国内可直连的 HF 镜像或 ModelScope);
    `available()` 只认"cn_clip 已装 + 权重已在本地",避免运行期(托盘用
    HF_HUB_OFFLINE=1 启动)在后台偷偷联网下载。
    """

    # cn_clip 1.6 的命名;2.x 支持 HF 名(如 OFA-Sys/chinese-clip-vit-base-p16)
    DEFAULT_MODEL = "ViT-B-16"
    _CHECKPOINT = {"ViT-B-16": "clip_cn_vit-b-16.pt",
                   "ViT-L-14": "clip_cn_vit-l-14.pt",
                   "RN50": "clip_cn_rn50.pt"}

    def __init__(self, model_name=None, download_root=None):
        self.model_name = model_name or self.DEFAULT_MODEL
        self.download_root = download_root or os.path.expanduser("~/.cache/clip")
        self._model = None
        self._processor = None
        self._device = "cpu"

    def _checkpoint_path(self) -> str:
        name = self._CHECKPOINT.get(self.model_name)
        if name is None:  # HF 名(cn_clip 2.x):交给它自己缓存
            return ""
        return os.path.join(self.download_root, name)

    def available(self) -> bool:
        try:
            import cn_clip  # noqa: F401
        except ImportError:
            return False
        path = self._checkpoint_path()
        if path and not os.path.isfile(path):
            logger.info("Chinese-CLIP 权重未下载(%s),图像嵌入跳过;"
                        "需要就联网跑一次 scripts/embed_images.py", path)
            return False
        return True

    def _ensure(self):
        if self._model is None:
            import torch
            from cn_clip.clip import load_from_name

            # 注意:cn_clip 的 CLIP 对象没有 .device 属性,这里自己记住设备
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
            self._model, self._processor = load_from_name(
                self.model_name, device=self._device,
                download_root=self.download_root)
            self._model.eval()
            logger.info("已加载 Chinese-CLIP %s(%s)", self.model_name, self._device)
        return self._model, self._processor

    def encode_image(self, image_path: str):
        from PIL import Image
        import torch

        model, processor = self._ensure()
        image = Image.open(image_path).convert("RGB")
        with torch.no_grad():
            feats = model.encode_image(processor(image).unsqueeze(0).to(self._device))
            feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats[0].cpu().tolist()

    def encode_image_batch(self, image_paths: list, batch_size: int = 8) -> list:
        """批量编码(建索引时用)。坏图/缺失返回 None。"""
        import torch
        from PIL import Image

        model, processor = self._ensure()
        out = []
        for i in range(0, len(image_paths), batch_size):
            group = image_paths[i:i + batch_size]
            tensors = []
            for p in group:
                try:
                    tensors.append(processor(Image.open(p).convert("RGB")))
                except Exception:  # noqa: BLE001 — 坏图跳过,不影响其余
                    tensors.append(None)
            valid = [t for t in tensors if t is not None]
            vecs = [None] * len(group)
            if valid:
                with torch.no_grad():
                    feats = model.encode_image(torch.stack(valid).to(self._device))
                    feats = feats / feats.norm(dim=-1, keepdim=True)
                it = iter(feats.cpu().tolist())
                vecs = [next(it) if t is not None else None for t in tensors]
            out.extend(vecs)
        return out

    def encode_text(self, texts: list) -> list:
        """把查询文本编码到同一向量空间(文字搜画面用)。

        注意:cn_clip 1.6 的 `processor` 只做**图像**预处理,文本要用 `tokenize`
        (原始 CLIP 风格 API);HF 风格的 processor(texts) 是 cn_clip 2.x 才有。
        """
        import torch
        from cn_clip.clip import tokenize

        model, _ = self._ensure()
        with torch.no_grad():
            feats = model.encode_text(tokenize(texts).to(self._device))
            feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats.cpu().tolist()
