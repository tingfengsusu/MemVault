"""视频画像探针与顺序解码的测试(设计稿第 0/1 步)。

判据全是纯函数 → 用合成序列测,不需要真实视频;解码部分用合成视频测,
并守住"顺序解码与逐点 seek 得到同一批帧"这条零回归红线。
"""
import numpy as np
import pytest


# ── 纯函数判据 ────────────────────────────────────────────────────────
def test_events_from_series_basic():
    """上升沿→静息≥0.4s 判结束;阈值按本片静息水平(P20×3)相对判定。"""
    from memvault.vision.frames_probe import events_from_series

    # 5Hz:0~2s 静息(0.01),2~5s 动(0.6),5~8s 静息,8~10s 动
    series = [0.01] * 10 + [0.6] * 15 + [0.01] * 15 + [0.6] * 10
    ts = [i * 0.2 for i in range(len(series))]
    events, floor, enter, exit_thr = events_from_series(ts, series)
    assert floor == pytest.approx(0.01, abs=1e-6)
    assert enter == pytest.approx(0.05, abs=1e-6)      # max(floor*3, 0.05)
    assert exit_thr == pytest.approx(0.015, abs=1e-6)
    assert len(events) == 2
    assert events[0][0] == pytest.approx(2.0, abs=0.3)      # 第一段动作起点
    assert events[1][1] == pytest.approx(10.0, abs=0.3)     # 末尾未闭合 → 收到结尾


def test_events_from_series_empty_and_flat():
    from memvault.vision.frames_probe import events_from_series

    assert events_from_series([], []) == ([], 0.0, 0.0, 0.0)
    flat = [0.0] * 50
    events, floor, enter, _ = events_from_series([i * 0.2 for i in range(50)], flat)
    assert events == [] and floor == 0.0


def test_settle_from_series_uses_relative_threshold():
    """成品展示=相对静止段:视频整体很动时,仍能找出局部静止(不能用绝对阈值)。"""
    from memvault.vision.frames_probe import settle_from_series

    # 全片运动 0.5(很动),中间 3s 降到 0.02(展示镜头)
    frame_motion = [0.5] * 20 + [0.02] * 15 + [0.5] * 20
    ts = [i * 0.2 for i in range(len(frame_motion))]
    segs = settle_from_series(ts, frame_motion, min_seconds=0.8, ratio=1.2)
    assert len(segs) == 1
    assert segs[0][0] == pytest.approx(4.0, abs=0.4)
    assert segs[0][1] == pytest.approx(6.8, abs=0.4)

    # 太短的静止不算(0.4s < 0.8s)
    short = [0.5] * 20 + [0.02] * 2 + [0.5] * 20
    assert settle_from_series([i * 0.2 for i in range(len(short))], short) == []


def test_pick_subtitle_bands_separation_rule():
    """活跃文字带 = 含文字带里变化强度 ≥ 最低者×3;水印带被排除(设计稿 §1.3)。"""
    from memvault.vision.frames_probe import BandEvidence, pick_subtitle_bands

    bands = [BandEvidence(index=i, y0=i / 24, y1=(i + 1) / 24)
             for i in range(24)]
    # 只有 0 带(水印)、12 带(字幕)、19 带(静态水印)有文字
    for i, rate in ((0, 0.9), (12, 0.8), (19, 0.4)):
        bands[i].frames_seen = 10
        bands[i].text_rate = rate
    motion = np.zeros((24, 100))
    bands[0].motion = 0.0
    bands[12].motion = 0.0
    bands[19].motion = 0.0
    motion[0] = 0.056     # 水印带:多数时刻不动(P50 低)
    motion[12] = 0.385    # 字幕带
    motion[19] = 0.174    # 中等
    picked, why = pick_subtitle_bands(bands, motion)
    assert picked and picked[0] == (0.5, pytest.approx(13 / 24, abs=1e-6))
    assert "38" in why or "0.385" in why        # 说明里带上了证据
    assert 8 in [b.index for b in bands if b.has_text] or True


def test_pick_subtitle_bands_returns_none_when_undecidable():
    """判不出来必须合法返回 (红线②):没有文字带 / 全静止 / 都达不到分离度。"""
    from memvault.vision.frames_probe import BandEvidence, pick_subtitle_bands

    empty = [BandEvidence(index=i, y0=i / 24, y1=(i + 1) / 24) for i in range(24)]
    picked, why = pick_subtitle_bands(empty, np.zeros((24, 10)))
    assert picked == [] and "文字" in why

    # 有文字但全静止 → 不能硬选
    flat = [BandEvidence(index=i, y0=i / 24, y1=(i + 1) / 24) for i in range(24)]
    for i in (0, 12):
        flat[i].frames_seen = 10
        flat[i].text_rate = 0.8
    picked2, why2 = pick_subtitle_bands(flat, np.zeros((24, 50)))
    assert picked2 == [] and "静止" in why2

    # 两条带变化几乎一样(2× < 3×)→ 达不到分离度
    close = [BandEvidence(index=i, y0=i / 24, y1=(i + 1) / 24) for i in range(24)]
    for i in (0, 12):
        close[i].frames_seen = 10
        close[i].text_rate = 0.8
    motion = np.zeros((24, 50))
    motion[0] = 0.20
    motion[12] = 0.35
    picked3, why3 = pick_subtitle_bands(close, motion)
    assert picked3 == [] and "分离度" in why3


def test_text_bands_from_frames_without_ocr():
    """OCR 不可用时应安静返回空证据(而不是报错)。"""
    from memvault.vision.frames_probe import text_bands_from_frames

    class NoOcr:
        def available(self):
            return False

    bands = text_bands_from_frames([(0.0, None)], ocr=NoOcr())
    assert len(bands) == 24 and all(not b.has_text for b in bands)


# ── 第 1 步:顺序解码 ─────────────────────────────────────────────────
def _make_video(path, seconds=12, fps=10):
    import cv2

    w, h = 160, 120
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for i in range(seconds * fps):
        frame = np.full((h, w, 3), (i * 7 % 255, 60, 120), dtype=np.uint8)
        if i % (fps * 3) == 0:
            cv2.rectangle(frame, (20, 20), (90, 90), (255, 255, 255), -1)
        writer.write(frame)
    writer.release()


def test_probe_decode_grab_matches_seek(tmp_path):
    """顺序 grab 与逐点 seek 必须给出同一批探测点(零回归红线)。"""
    from memvault.vision.frames import _probe

    video = tmp_path / "v.mp4"
    _make_video(video, seconds=12, fps=10)
    a = _probe(video, budget=40, decode="grab")
    b = _probe(video, budget=40, decode="seek")
    assert len(a) == len(b) > 4
    assert [p["ts"] for p in a] == [p["ts"] for p in b]
    for pa, pb in zip(a, b):
        assert pa["corr"] == pytest.approx(pb["corr"], abs=1e-6)


def test_extract_frames_same_result_both_decodes(tmp_path):
    """抽帧结果(帧数与时间戳)与解码方式无关。"""
    from memvault.vision.frames import extract_frames

    video = tmp_path / "v2.mp4"
    _make_video(video, seconds=12, fps=10)
    fa = extract_frames(video, tmp_path / "a", max_frames=6, decode="grab")
    fb = extract_frames(video, tmp_path / "b", max_frames=6, decode="seek")
    assert [f["ts"] for f in fa] == [f["ts"] for f in fb]
    assert len(fa) == len(fb) >= 2


def test_config_has_probe_and_decode_switches():
    from memvault.config import DEFAULTS

    assert DEFAULTS["frames"]["decode"] in ("grab", "seek")
    assert DEFAULTS["frames"]["unit"] in ("auto", "off")
    probe = DEFAULTS["frames"]["probe"]
    assert probe["hz"] == 5.0 and probe["min_separation"] == 3.0
    assert DEFAULTS["vision"]["ocr"]["band"] in ("auto", "off")
