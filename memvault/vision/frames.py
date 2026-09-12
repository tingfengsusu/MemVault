"""视觉:关键帧抽帧(HSV 场景检测,移植自 Video2Shop)+ 时间戳。"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_frames(video_path, out_dir, max_frames=16, frame_interval=5.0,
                   scene_threshold=0.45) -> list[dict]:
    """抽帧,返回 [{"ts": 秒, "path": jpg 路径}]。

    首选 HSV 直方图相关性场景检测(corr < threshold 视为切镜);
    结果不足 2 帧时回退固定间隔抽帧并合并。
    """
    import cv2

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("frame_*.jpg"):
        f.unlink()

    result = _by_scene_detection(
        video_path, out_dir, max_frames, scene_threshold
    )
    if len(result) <= 1:
        logger.info("场景检测镜头过少,回退固定间隔抽帧")
        result = _by_interval(video_path, out_dir, max_frames, frame_interval)
    logger.info("共抽取 %d 帧", len(result))
    return result


def _open(video_path):
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total / fps if fps > 0 else 0
    return cap, fps, total, duration


def _by_scene_detection(video_path, out_dir, max_frames, threshold):
    import cv2

    cap, fps, total, duration = _open(video_path)
    if duration <= 0 or total == 0:
        cap.release()
        logger.warning("无法读取视频信息,回退固定间隔抽帧")
        return _by_interval(video_path, out_dir, max_frames, 5.0)

    scan = max(1, int(fps / 2))  # 每 0.5s 扫一帧
    frames, prev_hist = [], None
    idx = 0
    while idx < total and len(frames) < max_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if not ret:
            idx += scan
            continue
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [50, 60], [0, 180, 0, 256])
        hist = cv2.normalize(hist, hist).flatten()
        changed = prev_hist is None or cv2.compareHist(
            prev_hist, hist, cv2.HISTCMP_CORREL
        ) < threshold
        if changed:
            p = out_dir / f"frame_{len(frames):04d}.jpg"
            cv2.imwrite(str(p), frame)
            frames.append({"ts": round(idx / fps, 2), "path": str(p)})
            prev_hist = hist
        idx += scan
    cap.release()
    return frames


def _by_interval(video_path, out_dir, max_frames, interval):
    import cv2

    cap, fps, total, duration = _open(video_path)
    if duration <= 0:
        cap.release()
        logger.error("无法读取视频时长")
        return []
    step = int(fps * interval)
    if duration / interval > max_frames:
        step = max(1, int(total / max_frames))
    frames = []
    for i, pos in enumerate(range(0, total, step)):
        if len(frames) >= max_frames:
            break
        cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        ret, frame = cap.read()
        if ret:
            p = out_dir / f"frame_{i:04d}.jpg"
            cv2.imwrite(str(p), frame)
            frames.append({"ts": round(pos / fps, 2), "path": str(p)})
    cap.release()
    return frames
