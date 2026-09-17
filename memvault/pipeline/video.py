"""视频采集管线:下载/本地文件 → 抽帧 → OCR 存档 → ASR → 入库 → 嵌入。"""
import json
import logging
import shutil
from pathlib import Path

from memvault.asr import whisper_asr
from memvault.vision import frames as frames_mod
from memvault.vision.ocr import OcrReader

logger = logging.getLogger(__name__)


def _is_bv(source: str) -> bool:
    s = source.lower()
    return "bilibili.com" in s or "b23.tv" in s or s.startswith("bv")


def ingest_video(source: str, memory, cfg: dict, domain: str = "general",
                 progress=print) -> int:
    """采集一条视频,返回 item_id。

    source:B站 URL/BV 号,或本地视频文件路径。
    memory:memvault.memory.Memory 实例。
    """
    from memvault.config import media_dir

    media = media_dir(cfg)
    progress(f"解析视频源: {source}")

    # 1) 获取视频文件
    temp_video = None
    if _is_bv(source):
        from memvault.sources.bili_downloader import BiliDownloader

        bcfg = cfg.get("bili", {})
        dl = BiliDownloader(
            cookies_path=bcfg.get("cookies_path"),
            output_dir=str(media / "_downloads"),
        )
        progress("下载 B站视频 ...")
        video_path = dl.download(source, quality=bcfg.get("quality", 64))
        temp_video = Path(video_path)
    else:
        video_path = Path(source)
        if not video_path.is_file():
            raise FileNotFoundError(f"视频文件不存在: {source}")

    stem = video_path.stem
    source_ref = source if _is_bv(source) else str(video_path.resolve())

    # 2) 建条目(原始 chunk 入库前先占位,保证"先入库后提取")
    item_id = memory.add_item(
        domain=domain, type_="video", title=stem,
        content_text=None, source_type="video", source_ref=source_ref,
        media_paths=[],
    )
    progress(f"条目已创建 id={item_id}")

    # 3) 抽帧(带时间戳)
    fcfg = cfg.get("frames", {})
    frames_dir = media / f"item_{item_id}" / "frames"
    progress("场景检测抽帧 ...")
    frame_list = frames_mod.extract_frames(
        video_path, frames_dir,
        max_frames=fcfg.get("max_frames", 16),
        frame_interval=fcfg.get("frame_interval", 5.0),
        scene_threshold=fcfg.get("scene_threshold", 0.45),
    )

    # 4) 逐帧入库:图像 chunk + OCR 文本 chunk(存档)
    ocr = OcrReader()
    for i, fr in enumerate(frame_list):
        memory.add_image_chunk(item_id, fr["path"], start_ts=fr["ts"], seq=i)
        text = ocr.read_text(fr["path"])
        if text:
            memory.add_text_chunk(
                item_id, f"[画面文字 {progress_fmt(fr['ts'])}]\n{text}",
                start_ts=fr["ts"], seq=i,
            )
    progress(f"帧入库 {len(frame_list)} 张(OCR {'已存档' if ocr.available() else '未启用'})")

    # 5) ASR 转写 → 带时间戳文本 chunk
    acfg = cfg.get("asr", {})
    progress("ASR 转写 ...")
    try:
        segments = whisper_asr.transcribe(
            video_path, model_size=acfg.get("model", "small"),
            device=acfg.get("device", "cpu"),
            compute_type=acfg.get("compute_type", "int8"),
            initial_prompt=acfg.get("initial_prompt"),
        )
    except Exception as e:  # noqa: BLE001 — ASR 失败不丢整条条目
        logger.warning("ASR 失败,仅保留画面信息: %s", e)
        segments = []
    for i, seg in enumerate(segments):
        memory.add_text_chunk(
            item_id, f"[语音 {progress_fmt(seg['start'])}]\n{seg['text']}",
            start_ts=seg["start"], end_ts=seg["end"], seq=100 + i,
        )
    progress(f"语音段入库 {len(segments)} 段")

    # 6) 汇总正文(检索兜底 + 面板预览)
    full_text = "\n".join(seg["text"] for seg in segments) or None
    memory.db.update_item_media(
        item_id, content_text=full_text,
        media_paths=[f["path"] for f in frame_list],
    )

    # 7) 清理临时视频(帧图保留)
    if temp_video is not None and not cfg.get("keep_video"):
        try:
            shutil.rmtree(temp_video.parent / "_downloads", ignore_errors=True)
            temp_video.unlink(missing_ok=True)
        except OSError:
            pass
    progress(f"完成: item_id={item_id}, chunks 已进入向量索引")
    return item_id


def progress_fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"
