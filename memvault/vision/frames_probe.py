"""视频画像探针 — 设计稿 `docs/design-frame-units.md` 第 0 步。

**一次解码**拿三份证据,产出"这个视频该怎么抽帧"的画像:

1. **像素证据**:顺序 `grab()` 解码(禁止逐点 seek,实测快 7 倍)。每个采样点算
   **24 条横带各自的全分辨率签名**(192×16)的帧间变化比例 —— 只有全分辨率才分得清
   "水印不动 / 字幕在换"(96×96 粗网格里水印会被背景运动污染:实测全片都在 40% 上,
   ×3 分离度判据直接失效)。同一次解码顺手留 N 帧原图给文字证据用。
2. **文字证据**:对上一步留的帧做**带框** OCR,只统计「哪条带上有字、出现率多少」——
   **不比较文字内容**(水印 OCR 文本不稳定,比内容会被骗,见设计稿 §1.3 坑②)。
3. **人声证据**:由调用方传入(ASR 的 speech_seconds / duration),复用 `should_ocr` 口径。

判定(设计稿 §1.3/§2.2):
- **活跃文字带** = 含文字的带里,变化强度 ≥ 最低者 × `min_separation`(实测分离度 8.5×/9×);
- 有文字但像素几乎不变 = 水印/台标 → 落在低变化那一端,被这条规则天然排除;
- 允许**多个**活跃带(字幕带 + 图表带);中部横带也在候选里(#14 字幕在画面正中 y≈50%);
- **判不出来是合法输出**:返回 `ok=False` + 原因,调用方按现状策略继续(设计稿红线②)。

三条踩过的坑(设计稿 §1.3,务必不要再犯):
① 按「文字框数量最多」选带 → 选中水印带且指标更好看(静默失败);
② 按「OCR 文本是否变化」选带 → 水印文本本身就不稳,照样被骗;
③ 用「变化强度 ≥ 全带 P75」当门槛 → 低运动视频上把字幕带挤掉,误判「无字幕带」。
"""
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── 常量(与设计稿实测口径一致)──────────────────────────────────────
PIX_THR = 12                # |灰阶差| > 12 记为"变化像素"
BAND_SIG = (192, 16)        # 每条带的全分辨率签名(设计稿 bench 同口径)
FRAME_SIG = (96, 96)        # 全幅缩略图签名(只用于"成品展示=相对静止"判定)
MIN_SEPARATION = 3.0        # 活跃文字带:变化强度 ≥ 最低者 ×3
TEXT_FRAME_MIN_RATE = 0.3   # 一条带 ≥30% 的采样帧上有字 → 算"含文字带"
EVENT_QUIET_SECONDS = 0.4   # 连续静息 ≥0.4s → 动作结束
SPEECH_RATIO_DEFAULT = 0.3  # 与 should_ocr 同口径


@dataclass
class BandEvidence:
    """一条横带的证据。y0/y1 为归一化画面高度(0~1)。"""
    index: int
    y0: float
    y1: float
    motion: float = 0.0     # 变化强度(该带签名帧间变化比例的 P50/中位)
    text_rate: float = 0.0  # 有文字帧的比例
    frames_seen: int = 0

    @property
    def has_text(self) -> bool:
        return self.frames_seen > 0 and self.text_rate >= TEXT_FRAME_MIN_RATE


@dataclass
class VideoProfile:
    """视频画像。`ok=False` 时调用方必须走现状策略(设计稿红线②)。"""
    ok: bool = False
    reason: str = ""
    duration: float = 0.0
    used_hz: float = 0.0
    probe_seconds: float = 0.0
    subtitle_bands: list[tuple[float, float]] = field(default_factory=list)
    watermark_bands: list[tuple[float, float]] = field(default_factory=list)
    bands: list[BandEvidence] = field(default_factory=list)
    floor: float = 0.0            # 带内变化强度的静息水平(P20)
    enter: float = 0.0            # 进入阈值(floor×3)
    exit: float = 0.0             # 退出阈值(floor×1.5)
    events: list[tuple[float, float]] = field(default_factory=list)
    settle_segments: list[tuple[float, float]] = field(default_factory=list)
    speech_ratio: float | None = None
    anomalies: list[str] = field(default_factory=list)

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def event_median(self) -> float:
        if not self.events:
            return 0.0
        durs = sorted(hi - lo for lo, hi in self.events)
        return durs[len(durs) // 2]

    @property
    def is_subtitle_led(self) -> bool:
        """字幕为主:有可用字幕带且人声占比不高(设计稿 §2.3 通道选择)。"""
        if not self.subtitle_bands:
            return False
        if self.speech_ratio is None:
            return True
        return self.speech_ratio < SPEECH_RATIO_DEFAULT

    def summary(self) -> dict:
        return {
            "ok": self.ok, "reason": self.reason,
            "duration": round(self.duration, 1),
            "probe_seconds": round(self.probe_seconds, 1),
            "used_hz": self.used_hz,
            "subtitle_bands": [(round(a, 3), round(b, 3))
                               for a, b in self.subtitle_bands],
            "watermark_bands": [(round(a, 3), round(b, 3))
                                for a, b in self.watermark_bands],
            "floor": round(self.floor, 4), "enter": round(self.enter, 4),
            "exit": round(self.exit, 4),
            "events": self.event_count,
            "event_median": round(self.event_median, 2),
            "settle_segments": len(self.settle_segments),
            "speech_ratio": (None if self.speech_ratio is None
                             else round(self.speech_ratio, 3)),
            "anomalies": self.anomalies,
        }


# ── 像素证据(单次解码:带签名 + 全幅签名 + 留帧给 OCR)────────────────
def scan_pixels(video_path, hz: float = 5.0, n_bands: int = 24,
                ocr_frames: int = 10):
    """顺序解码一遍,返回 (ts, band_motion[n_bands][n], frame_motion, duration, frames)。

    band_motion[b][i]:第 b 条带在全分辨率签名上的帧间变化比例(水印带显著更低)。
    frame_motion[i]:全幅缩略图的变化比例(成品展示判定复用,不再单独解码)。
    frames:[(ts, ndarray)] 均匀留的 N 帧原图,交给文字证据 OCR(避免第二次解码)。
    """
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    h_full = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1
    step = max(1, int(round(fps / max(0.5, hz))))
    rects = [(int(h_full * i / n_bands),
              max(int(h_full * (i + 1) / n_bands), int(h_full * i / n_bands) + 1))
             for i in range(n_bands)]
    keep_every = max(step, max(1, total // max(1, ocr_frames)))
    prev_band: list = [None] * n_bands
    prev_frame = None
    ts: list[float] = []
    band_series: list[list[float]] = [[] for _ in range(n_bands)]
    frame_series: list[float] = []
    sampled: list = []
    idx = 0
    while True:
        if not cap.grab():          # 只解封装,不解码(逐点 seek 会慢 7 倍)
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if ok:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                small = cv2.resize(gray, FRAME_SIG,
                                   interpolation=cv2.INTER_AREA).astype(np.int16)
                if prev_frame is not None:
                    frame_series.append(
                        float((np.abs(small - prev_frame) > PIX_THR).mean()))
                else:
                    frame_series.append(0.0)
                prev_frame = small
                for b, (y0, y1) in enumerate(rects):
                    sig = cv2.resize(gray[y0:y1], BAND_SIG,
                                     interpolation=cv2.INTER_AREA).astype(np.int16)
                    if prev_band[b] is not None:
                        band_series[b].append(
                            float((np.abs(sig - prev_band[b]) > PIX_THR).mean()))
                    else:
                        band_series[b].append(0.0)
                    prev_band[b] = sig
                ts.append(idx / fps)
        if idx % keep_every == 0 and len(sampled) < ocr_frames:
            ok, frame = cap.retrieve()
            if ok:
                sampled.append((idx / fps, frame.copy()))
        idx += 1
    cap.release()
    band_motion = (np.array(band_series, dtype=float) if ts
                   else np.zeros((n_bands, 0)))
    frame_motion = (np.array(frame_series, dtype=float) if frame_series
                    else np.zeros(0))
    duration = total / fps if fps > 0 and total > 0 else (ts[-1] if ts else 0.0)
    return ts, band_motion, frame_motion, float(duration), sampled


# ── 文字证据(在已留帧上做,不再解码)──────────────────────────────────
def text_bands_from_frames(sampled_frames, ocr=None, n_bands: int = 24,
                           tmp_dir=None) -> list[BandEvidence]:
    """对留帧做带框 OCR,统计每条带的"有文字帧比例"(只统计位置,不比较内容)。"""
    import cv2

    from memvault.vision.ocr import OcrReader

    bands = [BandEvidence(index=i, y0=i / n_bands, y1=(i + 1) / n_bands)
             for i in range(n_bands)]
    if not sampled_frames:
        return bands
    ocr = ocr or OcrReader()
    if not ocr.available():
        return bands

    import tempfile
    from pathlib import Path

    own_tmp = tmp_dir is None
    tmp_dir = Path(tmp_dir or tempfile.mkdtemp(prefix="mv_probe_"))
    seen = 0
    try:
        for k, (_, frame) in enumerate(sampled_frames):
            p = tmp_dir / f"probe_{k:02d}.jpg"
            cv2.imwrite(str(p), frame)
            hit = set()
            for ln in ocr.read_lines(str(p)):
                b0 = int(ln["y0"] * n_bands)
                b1 = min(n_bands - 1, int(ln["y1"] * n_bands))
                for b in range(max(0, b0), b1 + 1):
                    hit.add(b)
            for b in hit:
                bands[b].frames_seen += 1
            seen += 1
    finally:
        if own_tmp:
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)
    if seen:
        for b in bands:
            b.text_rate = b.frames_seen / seen
    return bands


# ── 判据(纯函数,可离线测)──────────────────────────────────────────
def classify_text_bands(bands: list[BandEvidence], motion,
                        min_separation: float = MIN_SEPARATION,
                        max_bands: int = 2):
    """把含文字的带分成 活跃(字幕)与 静态(水印/台标)。

    返回 (subtitle_bands, watermark_bands, 原因)。静态文字带要让全幅 OCR 通道排除掉
    —— 否则水印会顺着"全幅补文字"重新混进单元文本(实现时实测 36% 的单元带水印)。
    """
    import numpy as np

    text_bands = [b for b in bands if b.has_text]
    if not text_bands:
        return [], [], "探针没有在任何横带上发现文字"
    for b in text_bands:
        if motion.shape[1]:
            b.motion = float(np.percentile(motion[b.index], 50))
    lowest = min(b.motion for b in text_bands)
    if lowest <= 0:
        return [], [b for b in text_bands], "含文字带的强度全为 0(画面完全静止)"
    thresh = lowest * min_separation
    active = [b for b in text_bands if b.motion >= thresh]
    static = [b for b in text_bands if b.motion < thresh]
    if not active:
        return [], static, (f"没有带达到分离度门槛(最低 {lowest:.4f} × "
                            f"{min_separation} = {thresh:.4f})")
    active.sort(key=lambda b: -b.motion)
    picked = [(b.y0, b.y1) for b in active[:max_bands]]
    static_sorted = sorted(static, key=lambda b: b.y0)
    detail = ", ".join(f"{b.y0*100:.0f}-{b.y1*100:.0f}%(变化 {b.motion:.3f}, "
                       f"文字率 {b.text_rate:.0%})" for b in active[:max_bands])
    why = f"活跃文字带 {detail};分离度 {active[0].motion / lowest:.1f}×"
    if static_sorted:
        why += ";静态文字带(水印) " + ", ".join(
            f"{b.y0*100:.0f}-{b.y1*100:.0f}%" for b in static_sorted)
    return picked, [(b.y0, b.y1) for b in static_sorted], why


def pick_subtitle_bands(bands: list[BandEvidence], motion,
                        min_separation: float = MIN_SEPARATION,
                        max_bands: int = 2):
    """挑活跃文字带(兼容入口);细分信息见 classify_text_bands。"""
    import numpy as np

    picked, _static, why = classify_text_bands(bands, motion, min_separation,
                                              max_bands)
    return picked, why


def events_from_series(ts, series, quiet_seconds: float = EVENT_QUIET_SECONDS):
    """从"带内变化率序列"切动作事件,返回 (events, floor, enter, exit)。

    阈值全部相对本片静息水平(P20):进入 = max(floor×3, 0.05),退出 = floor×1.5,
    连续静息 ≥0.4s 判动作结束。**展示镜头是相对静止,绝对阈值不通用**(设计稿 §1.6)。
    """
    import numpy as np

    if len(series) == 0:
        return [], 0.0, 0.0, 0.0
    arr = np.asarray(series, dtype=float)
    floor = float(np.percentile(arr, 20))
    enter = max(floor * 3, 0.05)
    exit_thr = floor * 1.5
    dt = float(np.median(np.diff(ts))) if len(ts) > 1 else 0.2
    events, in_run, start, quiet = [], False, None, 0
    for i, v in enumerate(arr):
        if not in_run:
            if v > enter:
                in_run, start, quiet = True, i, 0
        else:
            quiet = 0 if v > exit_thr else quiet + 1
            if quiet * dt >= quiet_seconds:
                events.append((float(ts[start]), float(ts[max(start, i - quiet)])))
                in_run = False
    if in_run:
        events.append((float(ts[start]), float(ts[-1])))
    return events, floor, enter, exit_thr


def settle_from_series(ts, frame_motion, min_seconds: float = 0.8,
                       ratio: float = 1.2):
    """从全幅运动序列切"相对静止段"(成品展示候选,设计稿 §1.6)。"""
    import numpy as np

    if len(frame_motion) == 0:
        return []
    arr = np.asarray(frame_motion, dtype=float)
    floor = float(np.percentile(arr, 20))
    thr = max(floor * ratio, 0.01)
    # 以动作为主的视频里 P20 会贴着动作水平,于是全片都被判成"静止" ——
    # 门槛不低于典型运动水平时不硬判(设计稿红线②:允许"判不出来")
    if thr >= float(np.median(arr)):
        return []
    dt = float(np.median(np.diff(ts))) if len(ts) > 1 else 0.2
    need = max(1, int(round(min_seconds / dt)))
    segments, run_start = [], None
    for i, v in enumerate(arr):
        if v <= thr:
            run_start = i if run_start is None else run_start
        else:
            if run_start is not None and i - run_start >= need:
                segments.append((float(ts[run_start]), float(ts[i - 1])))
            run_start = None
    if run_start is not None and len(arr) - run_start >= need:
        segments.append((float(ts[run_start]), float(ts[-1])))
    return segments


def detect_events(video_path, band: tuple[float, float], hz: float = 5.0):
    """便捷封装:只为这条带扫一遍(管线里请复用 scan_pixels 的产物)。"""
    ts, motion, _, _, _ = scan_pixels(video_path, hz=hz,
                                      n_bands=max(1, int(round(1 / max(1e-3, band[1] - band[0])))))
    idx = min(int(band[0] * motion.shape[0]), max(0, motion.shape[0] - 1))
    return events_from_series(ts, motion[idx])


def detect_settle_segments(video_path, hz: float = 5.0, min_seconds: float = 0.8,
                           ratio: float = 1.2):
    """便捷封装:自己扫一遍(管线里请复用 scan_pixels 的 frame_motion)。"""
    ts, _, frame_motion, _, _ = scan_pixels(video_path, hz=hz)
    return settle_from_series(ts, frame_motion, min_seconds=min_seconds,
                              ratio=ratio)


# ── 总入口 ───────────────────────────────────────────────────────────
def probe_video(video_path, speech_seconds: float | None = None,
                ocr=None, hz: float = 5.0, n_bands: int = 24,
                text_frames: int = 10, min_separation: float = MIN_SEPARATION,
                duration: float | None = None) -> VideoProfile:
    """跑完整探针,返回 VideoProfile(ok=False 表示判不出来,调用方走现状)。"""
    import time

    prof = VideoProfile()
    t0 = time.perf_counter()
    try:
        ts, band_motion, frame_motion, dur, sampled = scan_pixels(
            video_path, hz=hz, n_bands=n_bands, ocr_frames=text_frames)
    except Exception as e:  # noqa: BLE001 — 探针失败绝不影响主流程
        prof.reason = f"像素扫描失败: {e}"
        prof.anomalies.append("scan_failed")
        return prof
    prof.duration = float(duration or dur or (ts[-1] if ts else 0.0))
    prof.used_hz = hz
    if band_motion.shape[1] < 4:
        prof.reason = "采样点太少,无法形成画像"
        prof.anomalies.append("too_few_samples")
        return prof

    try:
        bands = text_bands_from_frames(sampled, ocr=ocr, n_bands=n_bands)
    except Exception as e:  # noqa: BLE001
        prof.reason = f"文字证据失败: {e}"
        prof.anomalies.append("text_scan_failed")
        return prof
    prof.bands = bands

    picked, watermark, why = classify_text_bands(bands, band_motion,
                                                 min_separation=min_separation)
    prof.watermark_bands = watermark
    if not picked:
        prof.reason = why
        prof.anomalies.append("no_subtitle_band")
        prof.probe_seconds = time.perf_counter() - t0
        logger.info("探针未判出字幕带:%s", why)
        return prof

    prof.subtitle_bands = picked
    try:
        prof.settle_segments = settle_from_series(ts, frame_motion)
    except Exception as e:  # noqa: BLE001
        prof.anomalies.append(f"settle_failed: {e}")
    try:
        b = next(x for x in bands if (x.y0, x.y1) == picked[0])
        events, floor, enter, exit_thr = events_from_series(
            ts, band_motion[b.index])
        prof.events, prof.floor, prof.enter, prof.exit = events, floor, enter, exit_thr
    except Exception as e:  # noqa: BLE001
        prof.anomalies.append(f"events_failed: {e}")

    if speech_seconds is not None and prof.duration > 0:
        prof.speech_ratio = float(speech_seconds) / prof.duration
    prof.ok = bool(prof.subtitle_bands)
    prof.reason = why
    prof.probe_seconds = time.perf_counter() - t0
    logger.info("探针画像:%s", prof.summary())
    return prof

PROFILE_TOLERANCE = {
    "band_shift": 0.05,      # 字幕带位置允许偏移 5% 画面高
    "rate_ratio": 3.0,       # 字幕换行频率同量级(≤3 倍)
    "speech_delta": 0.35,    # 人声占比同档(±0.35)
}


def validate_against(profile: VideoProfile, cached: dict,
                     tol: dict | None = None) -> tuple[bool, str]:
    """拿本次探针结果校验"是否仍符合该 UP 的常规形态"(设计稿 §2.6)。

    返回 (是否符合, 说明)。任一项明显偏离就判不符合 → 调用方走全自动并提示用户。
    """
    tol = {**PROFILE_TOLERANCE, **(tol or {})}
    if not cached or not cached.get("subtitle_bands"):
        return False, "没有可用缓存"
    if not profile.subtitle_bands:
        return False, "本次没判出字幕带"
    cb, nb = cached["subtitle_bands"][0], profile.subtitle_bands[0]
    shift = abs(float(cb[0]) - float(nb[0]))
    if shift > tol["band_shift"]:
        return False, f"字幕带位置偏移 {shift*100:.0f}%(> {tol['band_shift']*100:.0f}%)"
    chz, nhz = cached.get("event_hz"), None
    if chz and profile.duration:
        nhz = profile.event_count / profile.duration
        ratio = max(nhz, chz) / max(1e-6, min(nhz, chz))
        if ratio > tol["rate_ratio"]:
            return False, f"事件频率差 {ratio:.1f} 倍(> {tol['rate_ratio']})"
    cs, ns = cached.get("speech_ratio"), profile.speech_ratio
    if cs is not None and ns is not None and abs(float(cs) - float(ns)) > tol["speech_delta"]:
        return False, f"人声占比差 {abs(cs-ns):.2f}(> {tol['speech_delta']})"
    return True, "符合该 UP 常规形态"


def profile_payload(profile: VideoProfile) -> dict:
    """把探针结果压成可缓存的画像(只存判定与校验要用的字段)。"""
    return {
        "subtitle_bands": [(round(a, 4), round(b, 4)) for a, b in profile.subtitle_bands],
        "watermark_bands": [(round(a, 4), round(b, 4)) for a, b in profile.watermark_bands],
        "event_count": profile.event_count,
        "event_hz": ((profile.event_count / profile.duration)
                     if profile.duration else None),
        "speech_ratio": profile.speech_ratio,
        "duration": round(profile.duration, 1),
        "hz": profile.used_hz,
    }
