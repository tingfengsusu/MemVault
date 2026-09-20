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


def test_switches_accept_yaml_booleans():
    """YAML 会把 on/off 解析成布尔 —— 开关判断必须兼容 False/'off'/None(实测踩过:
    按注释写 `snap: off` 拿到的是 False,与字符串 'off' 比较会静默失效)。"""
    from memvault.config import is_off
    from memvault.pipeline.video import _probe_enabled, _unit_enabled

    assert is_off(False) and is_off("off") and is_off("OFF") and is_off(None)
    assert not is_off(True) and not is_off("on") and not is_off("auto")

    # 配置里写 off(YAML→False)必须真的关掉
    assert _unit_enabled({"frames": {"unit": False}}) is False
    assert _unit_enabled({"frames": {"unit": "off"}}) is False
    assert _unit_enabled({"frames": {"unit": "auto"}}) is True
    assert _probe_enabled({"frames": {"probe": {"enabled": False}}}) is False
    assert _probe_enabled({"frames": {"probe": {"enabled": "auto"}}}) is True

    # OCR 三态:off(布尔或字符串)都不跑;on 强制
    from memvault.pipeline.video import should_ocr

    assert should_ocr({"vision": {"ocr": {"enabled": False}}}, 0, 100)[0] is False
    assert should_ocr({"vision": {"ocr": {"enabled": "off"}}}, 0, 100)[0] is False
    assert should_ocr({"vision": {"ocr": {"enabled": True}}}, 100, 100)[0] is True
    assert should_ocr({"vision": {"ocr": {"enabled": "on"}}}, 100, 100)[0] is True


def test_max_units_caps_unit_count():
    """长视频防成本:单元数超 frames.max_units 时均匀抽样(保持全片覆盖,只降密度)。"""
    from memvault.config import DEFAULTS

    assert DEFAULTS["frames"]["max_units"] >= 50        # 设计稿 §5 要求的上限

    # 抽样逻辑(与管线里同一算法):48 个单元压到 24 → 隔一个取一个
    units = [{"start": i * 5.0, "end": i * 5.0 + 3, "start_frame": i * 5.0,
              "end_frame": i * 5.0 + 3, "speech": []} for i in range(48)]
    max_units = 24
    last = len(units) - 1
    idx = sorted({round(i * last / (max_units - 1)) for i in range(max_units)})
    picked = [units[i] for i in idx]
    assert len(picked) == 24
    assert picked[0]["start"] == 0.0                      # 首个保留
    assert picked[-1]["start"] == last * 5.0              # 结尾也保留(成品展示常在这里)
    starts = [u["start"] for u in picked]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)


def test_decide_pipeline_four_quadrants():
    """四象限判据(设计稿 §2.3)——重点是"人声高但有字幕"必须跑字幕 OCR。

    实测样本:BV1ki4y1K7sf 人声 85%/无字幕带(语音为主);
    BV1Pe4y1s7pt 人声 98%/**有**字幕带(两者都有)——旧实现会漏掉它的字幕。
    """
    from memvault.pipeline.video import decide_pipeline

    cfg = {"vision": {"ocr": {"enabled": "auto", "speech_ratio": 0.3}}}
    # 字幕为主:人声低 + 有带 → 字幕带 + 全幅 + 单元化
    p1 = decide_pipeline(cfg, True, 0.07)
    assert (p1["ocr_band"], p1["ocr_full"], p1["unitize"]) == (True, True, True)
    assert p1["quadrant"] == "字幕为主"
    # 两者都有:人声高 + 有带 → 必须抓字幕(但不跑全幅,省成本)+ 单元化
    p2 = decide_pipeline(cfg, True, 0.98)
    assert (p2["ocr_band"], p2["ocr_full"], p2["unitize"]) == (True, False, True)
    assert p2["quadrant"] == "两者都有"
    # 语音为主:人声高 + 无带 → 都不做(现状行为,零回归)
    p3 = decide_pipeline(cfg, False, 0.85)
    assert (p3["ocr_band"], p3["ocr_full"], p3["unitize"]) == (False, False, False)
    assert p3["quadrant"] == "语音为主"
    # 纯动作/音乐:人声低 + 无带 → 全幅兜底 OCR(字幕可能被探针漏判),不单元化
    p4 = decide_pipeline(cfg, False, 0.02)
    assert (p4["ocr_band"], p4["ocr_full"], p4["unitize"]) == (False, True, False)
    assert p4["quadrant"] == "纯动作/音乐"
    # 开关:off 全关;on 强制
    off = decide_pipeline({"vision": {"ocr": {"enabled": False}}}, True, 0.1)
    assert off["ocr_band"] is False and off["unitize"] is False
    on = decide_pipeline({"vision": {"ocr": {"enabled": True}}}, False, 0.9)
    assert on["ocr_band"] is True and on["ocr_full"] is True
