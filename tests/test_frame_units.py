"""结构单元组装 + 双 OCR 通道的接线测试(设计稿 design-frame-units.md 第 2/3 步)。

判据是纯函数,直接测;管线接线用桩(不跑真解码)。
"""
import pytest


def _profile(events, settles=(), bands=((0.5, 0.542),), speech_ratio=0.09):
    from memvault.vision.frames_probe import VideoProfile

    p = VideoProfile(ok=True, reason="test", duration=100.0,
                     subtitle_bands=list(bands), events=list(events),
                     settle_segments=list(settles), speech_ratio=speech_ratio)
    return p


# ── 第 3 步:单元组装 ─────────────────────────────────────────────────
def test_build_units_pairs_frames_and_sentences():
    """每单元 = 起点/终点两帧 + 该区间的**句子级**语音(不用合并后的 40s 段)。"""
    from memvault.pipeline.video import _build_units

    segments = [
        {"start": 0.0, "end": 1.0, "text": "第一句"},
        {"start": 1.2, "end": 2.5, "text": "第二句"},
        {"start": 2.6, "end": 4.0, "text": "第三句"},
        {"start": 9.0, "end": 10.0, "text": "遥远的另一句"},
    ]
    prof = _profile(events=[(0.0, 3.0), (8.5, 10.2)])
    units = _build_units(prof, segments)
    assert len(units) == 2
    assert units[0]["start"] == 0.0 and units[0]["end_frame"] == 3.0
    assert units[0]["speech"] == ["第一句", "第二句", "第三句"]   # 区间交集
    assert units[1]["speech"] == ["遥远的另一句"]
    # 起止帧成对出现(每单元 2 帧)
    assert {u["start_frame"] for u in units} == {0.0, 8.5}


def test_build_units_uses_settle_as_ending_anchor():
    """事件结束后 3s 内的静止段 = 成品展示镜头,收尾帧用它(设计稿 §1.6/§2.4)。"""
    from memvault.pipeline.video import _build_units

    prof = _profile(events=[(0.0, 2.0)], settles=[(2.4, 3.6)])
    units = _build_units(prof, [])
    assert units[0]["end_frame"] == 3.6          # 收尾锚到展示镜头末端
    # 远处的静止段不认(超过 3s 窗口)
    prof2 = _profile(events=[(0.0, 2.0)], settles=[(9.0, 11.0)])
    assert _build_units(prof2, [])[0]["end_frame"] == 2.0


def test_build_units_skips_jitter():
    """过短的事件(~0.1s)不成单元,避免抖动碎成一堆块。"""
    from memvault.pipeline.video import _build_units

    prof = _profile(events=[(0.0, 0.1), (1.0, 3.0)])
    units = _build_units(prof, [])
    assert len(units) == 1 and units[0]["start"] == 1.0


def test_unit_and_probe_gates_read_config():
    """回退开关:frames.unit=off / frames.probe.enabled=off(红线①的逃生门)。"""
    from memvault.pipeline.video import _probe_enabled, _unit_enabled

    assert _unit_enabled({}) is True                       # 默认 auto
    assert _unit_enabled({"frames": {"unit": "off"}}) is False
    assert _probe_enabled({"frames": {"probe": {"enabled": "off"}}}) is False
    assert _probe_enabled({}) is True


# ── 第 2 步:双 OCR 通道 ──────────────────────────────────────────────
def test_ocr_band_crops_watermark(tmp_path):
    """字幕带裁切:带外(水印/台标)不进文本;带内文字照常识别。

    用真实 EasyOCR(权重已在本地);没有权重时跳过。
    """
    pytest.importorskip("easyocr")
    from PIL import Image, ImageDraw

    from memvault.vision.ocr import OcrReader

    r = OcrReader()
    if not r.available():
        pytest.skip("easyocr 未装")

    img = Image.new("RGB", (480, 852), (20, 20, 24))
    d = ImageDraw.Draw(img)
    d.text((320, 20), "bilibili", fill=(240, 240, 240))        # 台标(带外)
    d.text((300, 30), "胡仔一人食", fill=(230, 230, 230))       # 水印(带外)
    for dy in range(26):                                        # 字幕带里的大字
        d.line((60, 445 + dy, 420, 445 + dy), fill=(250, 250, 250), width=1)
    p = tmp_path / "frame.jpg"
    img.save(p)

    full = r.read_text(str(p))
    band = r.read_text(str(p), band=(0.5, 0.542))
    # PIL 默认字体很小,OCR 会把 bilibili 读成 bllibili 之类 —— 用关键子串判定
    assert "bili" in full.lower()                 # 全幅能看到台标
    assert "bili" not in band.lower()             # 带内看不到(裁切生效)
    # 带内应能读到大字块(画的是实心条,OCR 至少不该崩)
    assert isinstance(band, str)


def test_ocr_band_falls_back_when_too_narrow(tmp_path):
    """带太窄时退回整幅,避免"什么都识别不出"。"""
    from PIL import Image

    from memvault.vision.ocr import _load_band

    img = Image.new("RGB", (100, 400), (30, 30, 30))
    p = tmp_path / "x.jpg"
    img.save(p)
    out = _load_band(str(p), (0.5, 0.501))    # 0.4px 的带
    assert out.shape[1] == 100                # 仍是整幅宽
    assert out.shape[0] > 100                 # 高度=整幅(退回)


def test_in_any_band_filters_watermark_lines():
    """水印行按整幅坐标剔除(实测:全幅 100% 含水印 → 排除后 0%)。"""
    from memvault.vision.ocr import _in_any_band

    wm = {"text": "胡仔一人食", "y0": 0.023, "y1": 0.054}
    body = {"text": "水果冰淇淋万能公式2.0", "y0": 0.512, "y1": 0.549}
    assert _in_any_band(wm, [(0.0, 0.05)]) is True      # 水印带 → 剔除
    assert _in_any_band(body, [(0.0, 0.05)]) is False   # 正文行保留
    assert _in_any_band(wm, []) is False                # 没给带就不过滤
    assert _in_any_band(wm, None) is False


def test_classify_text_bands_splits_watermark():
    """探针要把"含文字但几乎不动"的带单独标出来(供全幅通道排除)。"""
    import numpy as np

    from memvault.vision.frames_probe import BandEvidence, classify_text_bands

    bands = [BandEvidence(index=i, y0=i / 24, y1=(i + 1) / 24) for i in range(24)]
    for i, rate in ((0, 0.9), (12, 0.7)):     # 0 带=水印,12 带=字幕
        bands[i].frames_seen = 10
        bands[i].text_rate = rate
    motion = np.zeros((24, 50))
    motion[0] = 0.056
    motion[12] = 0.385
    subs, marks, why = classify_text_bands(bands, motion)
    assert subs == [(pytest.approx(0.5, abs=1e-6), pytest.approx(13 / 24, abs=1e-6))]
    assert marks == [(pytest.approx(0.0, abs=1e-6), pytest.approx(1 / 24, abs=1e-6))]
    assert "水印" in why


def test_build_units_rounds_frame_ts():
    """单元帧时间戳取两位小数(与 write_frames_at 落盘的一致,否则查不到帧图)。"""
    from memvault.pipeline.video import _build_units

    prof = _profile(events=[(0.0, 3.14159), (8.500000001, 10.2)])
    units = _build_units(prof, [])
    assert units[0]["end_frame"] == 3.14
    assert units[1]["start_frame"] == 8.5
