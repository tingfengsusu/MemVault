"""ASR:faster-whisper 封装(懒加载,带时间戳分段)。"""
import logging
import os

# faster-whisper 模型经 huggingface_hub 下载,国内镜像提前设置;
# HF_HUB_DISABLE_XET:Xet 存储的 CAS 接口镜像无法代理(401),强制经典下载
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

logger = logging.getLogger(__name__)


def transcribe(video_path, model_size="small", device="cpu",
               compute_type="int8", initial_prompt: str | None = None) -> list[dict]:
    """转写音轨,返回 [{"start": 秒, "end": 秒, "text": 文本}]。

    initial_prompt:领域提示词,把专有名词/术语提前告诉模型,
    显著降低"ZCode→gcode"这类同音误识别。
    """
    from faster_whisper import WhisperModel

    logger.info("加载 faster-whisper 模型 %s(%s/%s)...",
                model_size, device, compute_type)
    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        str(video_path), vad_filter=True, language=None,
        initial_prompt=initial_prompt or None,
    )
    out = []
    for s in segments:
        text = s.text.strip()
        if text:
            out.append({"start": round(s.start, 2), "end": round(s.end, 2),
                        "text": text})
    logger.info("转写完成:检测语言 %s,共 %d 段", info.language, len(out))
    return out
