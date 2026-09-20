"""视觉:OCR(EasyOCR 懒加载)。

Video2Shop 中 OCR 只用来筛帧;这里升级为"存档"——
每帧识别文本连同时间戳作为 text chunk 入库。未安装 easyocr 时优雅跳过。

性能(本机 CPU 实测,480x852 竖屏帧):EasyOCR 默认参数 10.3s/帧,
`canvas_size=960, mag_ratio=1.0` 只要 1.06s/帧 且识别结果一致
(默认 canvas 2560 + mag 1.5 会把图放大后再检测,纯浪费)。
40 帧 ≈ 42s;有 CUDA 版 torch 时自动改用 GPU。
"""
import logging

logger = logging.getLogger(__name__)

CANVAS_SIZE = 960    # 检测阶段的最大边长(超过则缩放),越大越慢
MAG_RATIO = 1.0      # 不放大输入


class OcrReader:
    def __init__(self):
        self._reader = None
        self._checked = False
        self._ok = False

    def available(self) -> bool:
        if not self._checked:
            self._checked = True
            try:
                import easyocr  # noqa: F401
                self._ok = True
            except ImportError:
                logger.info("easyocr 未安装,OCR 存档跳过(可选依赖)")
        return self._ok

    @staticmethod
    def _use_gpu() -> bool:
        try:
            import torch
            return bool(torch.cuda.is_available())
        except Exception:  # noqa: BLE001 — 判断失败就当 CPU
            return False

    def _ensure(self):
        if self._reader is None:
            import easyocr

            gpu = self._use_gpu()
            logger.info("初始化 EasyOCR(ch_sim+en, %s, canvas=%d)...",
                        "gpu" if gpu else "cpu", CANVAS_SIZE)
            self._reader = easyocr.Reader(
                ["ch_sim", "en"], gpu=gpu, verbose=False
            )
        return self._reader

    def read_text(self, image_path: str, band: tuple[float, float] | None = None) -> str:
        """识别一张图,按行拼接文本。失败返回空串,不抛异常。

        band=(y0, y1) 归一化高度区间:先裁出该横带再识别(字幕带通道用,
        见 docs/design-frame-units.md §2.5 —— 裁带能直接把水印/台标切掉)。
        """
        if not self.available():
            return ""
        try:
            reader = self._ensure()
            img = _load_band(image_path, band)
            lines = reader.readtext(img, detail=1,
                                    canvas_size=CANVAS_SIZE,
                                    mag_ratio=MAG_RATIO)
            return "\n".join(t for _, t, _ in lines if t.strip())
        except Exception as e:  # noqa: BLE001 — 单帧失败不影响整条管线
            logger.warning("OCR 失败 %s: %s", image_path, e)
            return ""

    def read_lines(self, image_path: str,
                   band: tuple[float, float] | None = None) -> list[dict]:
        """带框识别:返回 [{text, y0, y1, conf}],y0/y1 为归一化画面高度。

        供探针统计"哪条带上有字"(只看位置不看内容)与双通道 OCR 使用。
        """
        if not self.available():
            return []
        try:
            reader = self._ensure()
            img = _load_band(image_path, band)
            h = float(img.shape[0]) or 1.0
            out = []
            for box, text, conf in reader.readtext(img, detail=1,
                                                   canvas_size=CANVAS_SIZE,
                                                   mag_ratio=MAG_RATIO):
                if not (text or "").strip():
                    continue
                ys = [float(p[1]) for p in box]
                y0, y1 = min(ys) / h, max(ys) / h
                if band:   # 还原到整幅坐标
                    y0 = band[0] + y0 * (band[1] - band[0])
                    y1 = band[0] + y1 * (band[1] - band[0])
                out.append({"text": text.strip(), "y0": y0, "y1": y1,
                            "conf": float(conf)})
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("OCR(带框)失败 %s: %s", image_path, e)
            return []


def _load_band(image_path: str, band: tuple[float, float] | None):
    """读图;给了 band 就裁出该横带并放大到原带宽的等价高度,便于小字识别。

    返回 numpy 数组:easyocr 只吃路径或 ndarray,PIL 对象会报 `no attribute shape`。
    """
    import numpy as np
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    if not band:
        return np.array(img)
    w, h = img.size
    y0 = max(0, int(h * float(band[0])))
    y1 = min(h, int(h * float(band[1])))
    if y1 - y0 < 8:      # 带太窄,退回整幅,避免识别不出任何东西
        return np.array(img)
    cropped = img.crop((0, y0, w, y1))
    scale = max(1.0, 96.0 / max(1, cropped.size[1]))   # 小带适当放大
    if scale > 1.0:
        cropped = cropped.resize((int(cropped.size[0] * scale),
                                  int(cropped.size[1] * scale)),
                                 Image.LANCZOS)
    return np.array(cropped)
