"""视觉:关键帧抽帧(HSV 直方图场景检测,移植自 Video2Shop)+ 时间戳。

与 Video2Shop 的关键差别:**抽帧必须覆盖整段视频**。原实现扫到 max_frames
就停,长视频(如 44 分钟)只采到开头几分钟;现在改为
「全片粗扫 → 时间轴分桶 → 每桶取画面变化最大的点」,
静态画面(字幕/PPT 类)的桶里没有变化点就取桶首,保证时间轴均匀铺满。
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PROBE_BUDGET = 600  # 全片粗扫采样点上限:够铺满长视频,又不会把解码拖太久


def video_duration(video_path) -> float:
    """视频时长(秒);读不出来返回 0。"""
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return total / fps if fps > 0 and total > 0 else 0.0


def extract_frames(video_path, out_dir, max_frames=40, frame_interval=5.0,
                   scene_threshold=0.45, decode="grab") -> list[dict]:
    """抽帧,返回 [{"ts": 秒, "path": jpg 路径}],时间轴从头铺到尾。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("frame_*.jpg"):
        f.unlink()

    probes = _probe(video_path, PROBE_BUDGET, decode=decode)
    if not probes:
        logger.warning("场景探测失败,回退固定间隔抽帧")
        return _by_interval(video_path, out_dir, max_frames, frame_interval)

    result = _write_frames(video_path, out_dir,
                           _pick_covering(probes, max_frames, scene_threshold))
    if len(result) <= 1:  # 探测到但写盘失败/帧太少 → 兜底
        logger.info("有效帧过少,回退固定间隔抽帧")
        return _by_interval(video_path, out_dir, max_frames, frame_interval)
    logger.info("共抽取 %d 帧(0.0s ~ %.0fs)", len(result), result[-1]["ts"])
    return result


def _probe(video_path, budget: int, decode: str = "grab") -> list[dict]:
    """全片粗扫:均匀取点,算相邻点的 HSV 直方图相关性(corr 越低=画面变化越大)。

    只记录 (ts, corr),不存图;选中的点再回头写盘。

    decode=`grab`(默认)顺序解码、只在采样点 `retrieve()`;`seek` 为旧的逐点
    `set(CAP_PROP_POS_FRAMES)`。设计稿实测顺序解码快 7 倍(17s → 4s),
    是零风险的免费收益;保留 seek 档只为回退。
    """
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if fps <= 0 or total <= 0:
        cap.release()
        return []
    step = max(1, total // max(1, budget))
    probes, prev_hist = [], None

    def _hist(frame):
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        h = cv2.calcHist([hsv], [0, 1], None, [50, 60], [0, 180, 0, 256])
        return cv2.normalize(h, h).flatten()

    if decode == "seek":
        for idx in range(0, total, step):
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok:
                continue
            hist = _hist(frame)
            corr = 1.0 if prev_hist is None else float(
                cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL))
            probes.append({"ts": round(idx / fps, 2), "corr": corr})
            prev_hist = hist
        cap.release()
        return probes

    idx = 0
    while True:
        if not cap.grab():      # 只解封装,不解码
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if ok:
                hist = _hist(frame)
                corr = 1.0 if prev_hist is None else float(
                    cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL))
                probes.append({"ts": round(idx / fps, 2), "corr": corr})
                prev_hist = hist
        idx += 1
    cap.release()
    return probes


def _pick_covering(probes: list[dict], max_frames: int,
                   threshold: float) -> list[dict]:
    """按时间把探测点分成 max_frames 个桶,每桶取画面变化最大的点。

    桶内最大变化也高于阈值(即整桶画面几乎没变)时取桶首,保证均匀覆盖。
    """
    if max_frames <= 0:
        return []
    if len(probes) <= max_frames:
        return probes
    picked = []
    n = len(probes)
    for b in range(max_frames):
        lo, hi = n * b // max_frames, max(1, n * (b + 1) // max_frames)
        bucket = probes[lo:hi]
        if not bucket:
            continue
        best = min(bucket, key=lambda p: p["corr"])
        picked.append(best if best["corr"] < threshold else bucket[0])
    return picked


def _write_frames(video_path, out_dir, picks: list[dict]) -> list[dict]:
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 1
    frames = []
    for i, p in enumerate(picks):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(p["ts"] * fps))
        ok, frame = cap.read()
        if not ok:
            continue
        path = out_dir / f"frame_{i:04d}.jpg"
        cv2.imwrite(str(path), frame)
        frames.append({"ts": p["ts"], "path": str(path)})
    cap.release()
    return frames


def _by_interval(video_path, out_dir, max_frames, interval) -> list[dict]:
    """兜底:固定间隔抽帧,长视频按 max_frames 均匀铺满整段。"""
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total / fps if fps > 0 else 0
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

def write_frames_at(video_path, out_dir, timestamps, prefix="unit") -> list[dict]:
    """按给定时间戳写帧(结构单元用),顺序解码取值,返回 [{"ts","path"}]。

    与 extract_frames 共用落盘命名,便于条目详情页按 seq 展示。
    """
    import cv2

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob(f"{prefix}_*.jpg"):
        f.unlink()
    wanted = sorted({round(float(t), 2) for t in timestamps})
    if not wanted:
        return []
    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    targets = {int(round(t * fps)): t for t in wanted}
    frames, idx = [], 0
    while targets:
        if not cap.grab():
            break
        if idx in targets:
            ok, frame = cap.retrieve()
            if ok:
                ts = targets.pop(idx)
                p = out_dir / f"{prefix}_{len(frames):04d}.jpg"
                cv2.imwrite(str(p), frame)
                frames.append({"ts": ts, "path": str(p)})
        idx += 1
    cap.release()
    return frames
