"""视觉:OCR(EasyOCR 懒加载)。

Video2Shop 中 OCR 只用来筛帧;这里升级为"存档"——
每帧识别文本连同时间戳作为 text chunk 入库。未安装 easyocr 时优雅跳过。
"""
import logging

logger = logging.getLogger(__name__)


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

    def _ensure(self):
        if self._reader is None:
            import easyocr

            logger.info("初始化 EasyOCR(ch_sim+en, cpu)...")
            self._reader = easyocr.Reader(
                ["ch_sim", "en"], gpu=False, verbose=False
            )
        return self._reader

    def read_text(self, image_path: str) -> str:
        """识别一张图,按行拼接文本。失败返回空串,不抛异常。"""
        if not self.available():
            return ""
        try:
            reader = self._ensure()
            lines = reader.readtext(str(image_path), detail=1)
            return "\n".join(t for _, t, _ in lines if t.strip())
        except Exception as e:  # noqa: BLE001 — 单帧失败不影响整条管线
            logger.warning("OCR 失败 %s: %s", image_path, e)
            return ""
