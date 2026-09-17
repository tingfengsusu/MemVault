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


def _merge_asr_segments(segments: list[dict], max_chars: int = 240,
                        max_seconds: float = 45.0) -> list[dict]:
    """把 whisper 的碎句合并成 ~40 秒的段落:块数降 5-8 倍,
    既大幅减少嵌入耗时,又让每块有完整语义(检索质量更好)。"""
    merged, buf, start, end = [], [], None, None
    for s in segments:
        if start is None:
            start = s["start"]
        buf.append(s["text"])
        end = s["end"]
        if len("".join(buf)) >= max_chars or (end - start) >= max_seconds:
            merged.append({"start": start, "end": end, "text": "".join(buf)})
            buf, start, end = [], None, None
    if buf:
        merged.append({"start": start, "end": end, "text": "".join(buf)})
    return merged


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

    # 幂等:同一来源重跑(中断恢复/重复采集)时先清理旧条目及其向量,
    # 避免中断重试产生重复条目
    old_ids = memory.db.delete_items_by_source_ref(source_ref)
    if old_ids:
        memory.vs.delete_by_item_ids(old_ids)
        progress(f"清理同源旧条目 {old_ids}(中断重跑)")

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

    # 5) ASR 转写 → 合并碎句 → 批量嵌入(一次编码代替数百次)
    acfg = cfg.get("asr", {})
    progress("ASR 转写 ...")
    try:
        segments = whisper_asr.transcribe(
            video_path, model_size=acfg.get("model", "small"),
            device=acfg.get("device", "auto"),
            compute_type=acfg.get("compute_type", "int8"),
            initial_prompt=acfg.get("initial_prompt"),
            beam_size=acfg.get("beam_size", 1),
            cpu_threads=acfg.get("cpu_threads", 0),
        )
    except Exception as e:  # noqa: BLE001 — ASR 失败不丢整条条目
        logger.warning("ASR 失败,仅保留画面信息: %s", e)
        segments = []
    merged = _merge_asr_segments(segments)
    memory.add_text_chunks_batch(item_id, [
        {"content": f"[语音 {progress_fmt(seg['start'])}]\n{seg['text']}",
         "start_ts": seg["start"], "end_ts": seg["end"], "seq": 100 + i}
        for i, seg in enumerate(merged)
    ])
    progress(f"语音段合并入库 {len(merged)} 段(原始 {len(segments)} 句)")

    # 6) 汇总正文(检索兜底 + 面板预览)
    full_text = "\n".join(seg["text"] for seg in merged) or None
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
