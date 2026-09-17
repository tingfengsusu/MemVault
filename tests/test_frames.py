"""抽帧覆盖 + OCR 触发条件测试。合成视频用 OpenCV 现造,不依赖真实素材。"""
import cv2
import numpy as np
import pytest


def make_video(path, seconds=20, fps=10, segments=4):
    """造一段分段变色的视频:每段颜色不同,段内基本静止(模拟讲稿画面)。"""
    w, h = 160, 120
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0),
              (0, 255, 255), (255, 0, 255)]
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"),
                             fps, (w, h))
    per = max(1, seconds * fps // segments)
    for i in range(seconds * fps):
        seg = min(i // per, len(colors) - 1)
        frame = np.full((h, w, 3), colors[seg], dtype=np.uint8)
        if i % per == per - 1:          # 段末加个框,制造变化
            cv2.rectangle(frame, (10, 10), (60, 60), (255, 255, 255), -1)
        writer.write(frame)
    writer.release()


@pytest.mark.skipif(cv2.VideoWriter is None, reason="no cv2")
def test_frames_cover_whole_video(tmp_path):
    """抽帧必须铺满整段视频,而不是只采开头(用户实测"40帧×3秒=只看了2分钟")。"""
    from memvault.vision.frames import extract_frames

    video = tmp_path / "v.mp4"
    make_video(video, seconds=20, fps=10, segments=4)
    frames = extract_frames(video, tmp_path / "frames", max_frames=8)

    assert 2 <= len(frames) <= 8
    ts = [f["ts"] for f in frames]
    assert ts == sorted(ts)                       # 时间有序
    assert ts[-1] >= 20 * 0.6                     # 末尾有帧(不是只看开头)
    assert ts[0] <= 20 * 0.2                      # 开头也有
    assert len({round(t / 5) for t in ts}) >= 3   # 分布在多个时间段
    assert all((tmp_path / "frames").glob("*.jpg"))


def test_frames_fallback_interval_covers_all(tmp_path):
    """无法读时长/探测失败时的兜底间隔抽帧也要铺满整段。"""
    from memvault.vision.frames import _by_interval

    video = tmp_path / "v2.mp4"
    make_video(video, seconds=20, fps=10, segments=2)
    frames = _by_interval(video, tmp_path / "f2", max_frames=5, interval=1.0)
    assert 3 <= len(frames) <= 5
    assert frames[-1]["ts"] >= 20 * 0.6


def test_should_ocr_modes():
    """OCR 触发条件:off 不跑 / on 一律跑 / auto 只在人声很少时跑。"""
    from memvault.pipeline.video import should_ocr

    cfg = {"vision": {"ocr": {"enabled": "off"}}}
    assert should_ocr(cfg, speech_seconds=0, duration=300)[0] is False

    cfg = {"vision": {"ocr": {"enabled": "on"}}}
    assert should_ocr(cfg, speech_seconds=300, duration=300)[0] is True

    cfg = {"vision": {"ocr": {"enabled": "auto", "speech_ratio": 0.3}}}
    # 真实 #14:6分31秒视频只有 36 秒人声 → 判为字幕视频,跑 OCR
    assert should_ocr(cfg, speech_seconds=36, duration=391)[0] is True
    # 正常解说视频(44 分钟几乎全程有人声)→ 跳过 OCR,省一遍识别
    assert should_ocr(cfg, speech_seconds=2400, duration=2640)[0] is False
    # 没有语音的纯画面视频 → 跑
    assert should_ocr(cfg, speech_seconds=0, duration=120)[0] is True
    # 时长为 0(读不出来)时保守跳过,避免无谓 OCR
    assert should_ocr(cfg, speech_seconds=0, duration=0)[0] is False


def test_config_defaults_have_ocr_and_frames():
    from memvault.config import DEFAULTS

    assert DEFAULTS["vision"]["ocr"]["enabled"] == "auto"
    assert DEFAULTS["frames"]["max_frames"] >= 40   # 16 覆盖不了长视频
