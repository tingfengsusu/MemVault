"""文件采集处理器(热键剪贴板路径 / 面板上传)。

视频文件 → 视频管线;图片 → 图像条目;其他 → 待整理(M3 接 unstructured)。
"""
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

VIDEO_EXT = {".mp4", ".mkv", ".flv", ".avi", ".mov", ".webm", ".m4s"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def ingest_file(payload: dict, memory, cfg: dict) -> int:
    """payload: {path}"""
    src = Path(payload["path"])
    if not src.is_file():
        raise FileNotFoundError(f"文件不存在: {src}")

    ext = src.suffix.lower()
    if ext in VIDEO_EXT:
        from memvault.pipeline.video import ingest_video

        return ingest_video(str(src), memory, cfg, domain=payload.get("domain", "general"))

    if ext in IMAGE_EXT:
        return _ingest_image(src, memory, cfg, payload)

    # 其他文档:占位条目进待整理箱,正文解析 M3 接入
    item_id = memory.add_item(
        domain=payload.get("domain", "general"),
        type_="file",
        title=src.name,
        source_type="file",
        source_ref=str(src.resolve()),
    )
    memory.db.update_item_media(item_id, content_text="(M3 文档解析待接入)")
    logger.info("文件占位入库 item=%s (%s)", item_id, src.name)
    return item_id


def _ingest_image(src: Path, memory, cfg: dict, payload: dict) -> int:
    from memvault.config import media_dir

    # 复制进数据目录,原文件可被用户移动/删除
    dst_dir = media_dir(cfg) / "item_manual"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{src.stem}_{src.stat().st_size}{src.suffix}"
    shutil.copy2(src, dst)

    item_id = memory.add_item(
        domain=payload.get("domain", "general"),
        type_="image",
        title=src.stem,
        media_paths=[str(dst)],
        source_type="file",
        source_ref=str(src.resolve()),
    )
    memory.add_image_chunk(item_id, str(dst))
    return item_id


def capture_from_clipboard(memory, cfg: dict) -> dict:
    """热键入口:读剪贴板,是路径→文件任务,是文本→文本任务。

    返回 {ok, kind, detail} 供托盘提示。
    """
    import pyperclip

    try:
        content = pyperclip.paste().strip()
    except pyperclip.PyperclipException as e:
        return {"ok": False, "kind": "error", "detail": f"剪贴板读取失败: {e}"}
    if not content:
        return {"ok": False, "kind": "empty", "detail": "剪贴板为空"}

    # 多行文本可能是复制的文件列表,也可能是纯文本;逐行判断
    first_line = content.splitlines()[0].strip('"')
    maybe_path = Path(first_line)
    if maybe_path.exists() and not content.startswith("http"):
        import hashlib

        key = hashlib.sha1(("file|" + str(maybe_path.resolve())).encode()).hexdigest()[:16]
        memory.db.enqueue("ingest_file", {"path": str(maybe_path)}, dedup_key=key)
        return {"ok": True, "kind": "file", "detail": maybe_path.name}

    if content.startswith("http"):
        # URL:视频站直接走视频采集,其余做页面采集(正文由 M3 服务端抓取,M2 先存 URL 摘录)
        import hashlib

        is_video = any(h in content for h in ("bilibili.com/video", "b23.tv"))
        if is_video:
            key = hashlib.sha1(("video|" + content).encode()).hexdigest()[:16]
            memory.db.enqueue("ingest_video", {"source": content}, dedup_key=key)
            return {"ok": True, "kind": "video", "detail": content[:60]}
        key = hashlib.sha1(("link|" + content).encode()).hexdigest()[:16]
        memory.db.enqueue("ingest_text", {"title": content[:60], "url": content,
                                          "text": f"链接:{content}"},
                          dedup_key=key)
        return {"ok": True, "kind": "link", "detail": content[:60]}

    # 纯文本:内容哈希去重(同一段文字连按热键不会重复入库)
    import hashlib

    key = hashlib.sha1(("text|" + content).encode()).hexdigest()[:16]
    title = content.strip().splitlines()[0].strip()[:40] or "剪贴板摘录"
    existed = memory.db.job_exists(key)
    memory.db.enqueue("ingest_text",
                      {"title": title, "text": content, "kind": "selection"},
                      dedup_key=key)
    return {"ok": True, "kind": "text",
            "detail": ("已采集过(去重):" if existed else "") + title}
