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

    def read_text(self, image_path: str) -> str:
        """识别一张图,按行拼接文本。失败返回空串,不抛异常。"""
        if not self.available():
            return ""
        try:
            reader = self._ensure()
            lines = reader.readtext(str(image_path), detail=1,
                                    canvas_size=CANVAS_SIZE,
                                    mag_ratio=MAG_RATIO)
            return "\n".join(t for _, t, _ in lines if t.strip())
        except Exception as e:  # noqa: BLE001 — 单帧失败不影响整条管线
            logger.warning("OCR 失败 %s: %s", image_path, e)
            return ""
