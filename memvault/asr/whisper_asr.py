"""ASR:faster-whisper 封装(懒加载,带时间戳分段)。

device='auto':检测到 CUDA 时用 GPU(float16,快 10 倍以上),
否则/加载失败时回退 CPU(int8)。
"""
import logging
import os
import sys
from pathlib import Path

# faster-whisper 模型经 huggingface_hub 下载,国内镜像提前设置;
# HF_HUB_DISABLE_XET:Xet 存储的 CAS 接口镜像无法代理(401),强制经典下载
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

logger = logging.getLogger(__name__)


def _ensure_cuda_dlls():
    """Windows:让 CTranslate2 能找到 pip 安装的 CUDA 库(cublas/cudnn)。

    仅 add_dll_directory 不够(实测报 cublas64_12.dll not found):
    需要三管齐下——加 DLL 搜索目录 + 写 PATH + ctypes 预加载。
    """
    if os.name != "nt":
        return
    import ctypes

    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    dirs = [str(base / pkg / sub)
            for pkg in ("cublas", "cudnn")
            for sub in ("bin", "lib")
            if (base / pkg / sub).exists()]
    if not dirs:
        return
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except OSError:
            pass
    os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
    for name in ("cublasLt64_12.dll", "cublas64_12.dll", "cudnn64_9.dll"):
        try:
            ctypes.WinDLL(name)
        except OSError:
            pass


def _resolve_device(device: str, compute_type: str) -> tuple[str, str]:
    """device='auto' 时:有 CUDA 用 GPU(float16),否则 CPU(int8)。"""
    if device != "auto":
        return device, compute_type
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:  # noqa: BLE001 — 探测失败按 CPU 处理
        pass
    return "cpu", ("int8" if compute_type == "auto" else compute_type)


def transcribe(video_path, model_size="small", device="auto",
               compute_type="int8", initial_prompt: str | None = None,
               beam_size: int = 1, cpu_threads: int = 0) -> list[dict]:
    """转写音轨,返回 [{"start": 秒, "end": 秒, "text": 文本}]。

    device='auto':优先 GPU(CUDA 缺失时自动回退 CPU)。
    beam_size:1=greedy(快);5=beam search(略准但慢约 2 倍)。
    """
    from faster_whisper import WhisperModel

    device, compute_type = _resolve_device(device, compute_type)
    if device == "cuda":
        _ensure_cuda_dlls()
    kwargs = {"device": device, "compute_type": compute_type}
    if cpu_threads > 0 and device == "cpu":
        kwargs["cpu_threads"] = cpu_threads
    logger.info("加载 faster-whisper %s(%s/%s, beam=%d)...",
                model_size, device, compute_type, beam_size)
    try:
        model = WhisperModel(model_size, **kwargs)
    except Exception as e:  # noqa: BLE001 — GPU 加载失败回退 CPU
        if device == "cuda":
            logger.warning("CUDA 初始化失败(%s),回退 CPU", str(e)[:100])
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
        else:
            raise
    segments, info = model.transcribe(
        str(video_path), vad_filter=True, language=None,
        initial_prompt=initial_prompt or None, beam_size=beam_size,
        condition_on_previous_text=False,
    )
    out = []
    for s in segments:
        text = s.text.strip()
        if text:
            out.append({"start": round(s.start, 2), "end": round(s.end, 2),
                        "text": text})
    logger.info("转写完成:检测语言 %s,共 %d 段", info.language, len(out))
    return out
