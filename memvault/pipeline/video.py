"""视频采集管线:下载/本地文件 → 抽帧 → OCR 存档 → ASR → 入库 → 嵌入。"""
import json
import logging
import shutil
from pathlib import Path

from memvault.asr import whisper_asr
from memvault.vision import frames as frames_mod
from memvault.vision import frames_probe as frames_mod_probe
from memvault.vision.ocr import OcrReader

logger = logging.getLogger(__name__)


def _is_bv(source: str) -> bool:
    s = source.lower()
    return "bilibili.com" in s or "b23.tv" in s or s.startswith("bv")


def _image_embed_enabled(cfg: dict) -> bool:
    """是否给抽到的帧建图像向量(config.vision.image_embed.enabled: auto/on/off)。"""
    mode = str(((cfg.get("vision") or {}).get("image_embed") or {})
               .get("enabled", "auto")).lower()
    return mode != "off"


def _probe_enabled(cfg: dict) -> bool:
    """是否跑视频画像探针(config.frames.probe.enabled: auto|on|off)。

    auto(默认):只在"本来就要做 OCR 的视频"上跑 —— 旁白视频零额外成本(红线①)。
    """
    from memvault.config import is_off

    return not is_off(((cfg.get("frames") or {}).get("probe") or {})
                      .get("enabled", "auto"))


def _unit_enabled(cfg: dict) -> bool:
    """是否按"结构单元"落库(config.frames.unit: auto|off)。"""
    from memvault.config import is_off

    return not is_off((cfg.get("frames") or {}).get("unit", "auto"))


# 单元数下限:低于它说明探针没抓住结构,宁可回退到均匀抽帧(红线②)
_MIN_UNITS = 5
_SETTLE_WINDOW = 3.0    # 事件结束后 3s 内的静止段算"成品展示"


def _build_units(profile, segments: list[dict]) -> list[dict]:
    """把探针的动作事件 + 成品展示段 + 句子级语音组装成单元(纯函数,可离线测)。

    每单元:{"start", "end", "start_frame", "end_frame", "speech": [...]}
    - 起始帧 = 事件起点(动作开始);收尾帧 = 事件终点,若紧随其后有静止段则取其末尾
      (成品展示镜头,设计稿 §1.6/§2.4);
    - speech = 与该区间相交的**句子级** ASR 片段(不用合并后的 40s 段)。
    """
    settles = sorted(profile.settle_segments)
    units = []
    for start, end in profile.events:
        end_frame = end
        for lo, hi in settles:
            if end <= lo <= end + _SETTLE_WINDOW:
                end_frame = hi
                break
        if end_frame - start < 0.2:      # 太短的抖动不成单元
            continue
        words = [s["text"] for s in segments
                 if s.get("text") and s["end"] > start and s["start"] < end_frame]
        # 帧时间戳与 write_frames_at 一样取两位小数,否则查不到对应帧
        units.append({"start": float(start), "end": float(end_frame),
                      "start_frame": round(float(start), 2),
                      "end_frame": round(float(end_frame), 2),
                      "speech": words})
    return units


def decide_pipeline(cfg: dict, has_band: bool, speech_ratio: float) -> dict:
    """四象限判据(设计稿 §2.3):画像事实决定"要不要 OCR、要不要单元化"。

    | 画像 | OCR 字幕带通道 | OCR 全幅通道 | 单元化 |
    |---|---|---|---|
    | 人声低 + 有字幕带(字幕为主) | 要 | 要(兜底) | 要 |
    | 人声高 + 有字幕带(两者都有) | **要**(此前会漏) | 不要(省成本) | 要 |
    | 人声高 + 无字幕带(语音为主) | 不要 | 不要 | 不要 |
    | 人声低 + 无字幕带(纯动作/音乐) | 不要 | 要(兜底) | 不要 |

    旧实现只看"人声占比 <0.3 才 OCR":用户给的 BV1Pe4y1s7pt 人声 98%、字幕带在画面
    4~12.5%,于是**字幕永远进不了库** —— 这正是"两者都有"象限要求的双通道。
    """
    from memvault.config import is_off

    ocfg = (cfg.get("vision") or {}).get("ocr") or {}
    mode = ocfg.get("enabled", "auto")
    threshold = float(ocfg.get("speech_ratio", 0.3))
    low_speech = speech_ratio < threshold
    if is_off(mode):
        return {"ocr_band": False, "ocr_full": False, "unitize": False,
                "quadrant": "OCR 已关闭"}
    if str(mode).lower() in ("on", "true", "yes", "always"):
        return {"ocr_band": True, "ocr_full": True, "unitize": has_band,
                "quadrant": "OCR 强制开启"}
    quadrant = (("字幕为主" if low_speech else "两者都有") if has_band
                else ("语音为主" if not low_speech else "纯动作/音乐"))
    return {
        "ocr_band": has_band,      # 有字幕带就抓字幕(不再看人声多少)
        "ocr_full": low_speech,    # 全幅只在人声低时补(省成本)
        "unitize": has_band,       # 有字幕带 = 有结构可依,走单元化
        "quadrant": quadrant,
    }


def should_ocr(cfg: dict, speech_seconds: float,
               duration: float) -> tuple[bool, str]:
    """要不要对抽到的帧跑 OCR,返回 (是否, 原因)。

    config.vision.ocr.enabled:
      off  — 不跑
      on   — 一律跑(PPT/代码/图表类视频)
      auto — 先看 ASR:人声时长占比低于 speech_ratio 才跑。字幕/无配音视频
             正是这种情况(#14:6分31秒里只有 36 秒人声),而有正常解说的
             视频不必做第二遍识别(OCR + ASR 双份成本)。
    """
    ocfg = (cfg.get("vision") or {}).get("ocr") or {}
    from memvault.config import is_off

    mode = str(ocfg.get("enabled", "auto")).lower()
    if is_off(mode):
        return False, "config.vision.ocr.enabled=off"
    if str(mode).lower() in ("on", "true", "yes", "always"):
        return True, "config.vision.ocr.enabled=on(强制)"
    ratio = speech_seconds / duration if duration > 0 else 1.0
    limit = float(ocfg.get("speech_ratio", 0.3))
    if ratio < limit:
        return True, f"人声仅占 {ratio:.0%} < {limit:.0%},判为字幕/无配音视频"
    return False, f"人声占 {ratio:.0%} ≥ {limit:.0%},无需 OCR"


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
    meta: dict = {}
    if _is_bv(source):
        from memvault.sources.bili_downloader import BiliDownloader

        bcfg = cfg.get("bili", {})
        dl = BiliDownloader(
            cookies_path=bcfg.get("cookies_path"),
            output_dir=str(media / "_downloads"),
        )
        progress("下载 B站视频 ...")
        video_path = dl.download(source, quality=bcfg.get("quality", 64))
        meta = dict(getattr(dl, "last_meta", {}) or {})   # UP主/BV 号
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
    #    attrs_json 存采集时的原始属性:UP主/BV 号(详情页"原始属性"块可见;
    #    UP主还用于"该 UP 的视频自动归到某分类"的规则路由)
    attrs = {k: v for k, v in (("up", meta.get("up")),
                               ("up_mid", meta.get("up_mid")),
                               ("bvid", meta.get("bvid"))) if v}
    item_id = memory.add_item(
        domain=domain, type_="video", title=stem,
        content_text=None, source_type="video", source_ref=source_ref,
        media_paths=[], attrs=attrs,
    )
    progress(f"条目已创建 id={item_id}" + (f"(UP主 {attrs['up']})" if attrs.get("up") else ""))

    # 3) 抽帧(带时间戳,全片覆盖)。帧**先不入库**:字幕类视频会在第 6 步用
    #    "结构单元帧"替换掉这批均匀帧(省一次无用的图像向量计算)
    fcfg = cfg.get("frames", {})
    frames_dir = media / f"item_{item_id}" / "frames"
    progress("场景检测抽帧 ...")
    frame_list = frames_mod.extract_frames(
        video_path, frames_dir,
        max_frames=fcfg.get("max_frames", 40),
        frame_interval=fcfg.get("frame_interval", 5.0),
        scene_threshold=fcfg.get("scene_threshold", 0.45),
        decode=fcfg.get("decode", "grab"),
    )
    ib = memory.image_embedder if _image_embed_enabled(cfg) else None

    # 4) ASR 转写 → 合并碎句(合并段只用于旧路径;单元路径要句子级)
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

    # 5) OCR 决策 + 视频画像探针(设计稿 design-frame-units.md)
    #    探针只为"要做 OCR 的视频"跑:旁白视频零额外成本(红线①)
    speech_seconds = sum(max(0.0, s["end"] - s["start"]) for s in segments)
    duration = frames_mod.video_duration(video_path)
    want_ocr, reason = should_ocr(cfg, speech_seconds, duration)
    ocr = OcrReader() if want_ocr else None
    profile = None
    cached_profile = None
    if want_ocr and meta.get("up_mid"):
        cached_profile = memory.db.frames_profile(meta["up_mid"])
    if want_ocr and _probe_enabled(cfg) and cached_profile:
        # 该 UP 有缓存画像:先按缓存判断能否直接复用(省一次"文字证据"OCR)
        progress(f"该 UP 有画像缓存(字幕带 {cached_profile['subtitle_bands']}),"
                 f"本次仍跑探针做校验")
        profile = frames_mod_probe.probe_video(
            video_path, speech_seconds=speech_seconds, ocr=ocr,
            hz=float((fcfg.get("probe") or {}).get("hz", 5.0)),
            text_frames=int((fcfg.get("probe") or {}).get("text_frames", 10)),
            min_separation=float(
                (fcfg.get("probe") or {}).get("min_separation", 3.0)))
        progress(f"视频画像:字幕带 {profile.subtitle_bands or '未判出'} / "
                 f"事件 {profile.event_count} 个 / {profile.probe_seconds:.0f}s"
                 + (f" / 异常 {profile.anomalies}" if profile.anomalies else ""))
        # 第 4 步:与缓存画像比对 —— 不符合该 UP 常规形态时提示用户(仍继续,用本次画像)
        if cached_profile:
            from memvault.vision.frames_probe import validate_against

            ok_cached, why = validate_against(profile, cached_profile)
            if ok_cached:
                progress(f"画像校验:{why}(该 UP 的缓存可用)")
            else:
                progress(f"⚠ 不符合该 UP 常规形态:{why} —— 按全自动处理并提示")
                profile.anomalies.append(f"up_profile_mismatch: {why}")

    # 6) 四象限判据 → 结构单元化(有字幕带就有结构可依)+ 决定 OCR 通道
    has_band = bool(profile is not None and profile.subtitle_bands)
    plan = decide_pipeline(cfg, has_band, speech_ratio)
    if profile is not None:
        progress(f"画像判定:{plan['quadrant']}(字幕带{'有' if has_band else '无'}"
                 f" / 人声 {speech_ratio:.0%})→ 字幕带通道 {plan['ocr_band']} · "
                 f"全幅通道 {plan['ocr_full']} · 单元化 {plan['unitize']}")
        if plan["ocr_band"] and ocr is None:
            ocr = OcrReader()            # 有字幕带就必须跑 OCR(哪怕人声 98%)
            want_ocr = True
    units = []
    if profile is not None and _unit_enabled(cfg) and plan["unitize"]:
        units = _build_units(profile, segments)
        max_units = int((cfg.get("frames") or {}).get("max_units", 120) or 0)
        if max_units and len(units) > max_units:
            # 等距抽样但**首尾都保留**(结尾常是成品展示,不能丢)
            last = len(units) - 1
            idx = [round(i * last / (max_units - 1)) for i in range(max_units)]
            units = [units[i] for i in sorted(set(idx))]
            progress(f"单元数超上限,均匀抽样到 {len(units)} 个(max_units={max_units})")
        if len(units) >= _MIN_UNITS:
            ts_list = sorted({t for u in units for t in (u["start_frame"], u["end_frame"])})
            for old in frames_dir.glob("frame_*.jpg"):   # 清掉均匀帧,避免孤儿
                old.unlink()
            frame_list = frames_mod.write_frames_at(video_path, frames_dir, ts_list)
            progress(f"单元化: {len(units)} 个单元 → {len(frame_list)} 帧")
        else:
            units = []
            logger.info("单元数过少(%d < %d),已回退到均匀抽帧", len(units),
                        _MIN_UNITS)
    elif profile is not None and _unit_enabled(cfg):
        progress("画像显示为旁白视频,按均匀抽帧(未单元化)")
    if not units:
        progress("按均匀抽帧入库")

    for i, fr in enumerate(frame_list):
        memory.add_image_chunk(item_id, fr["path"], start_ts=fr["ts"], seq=i,
                               image_embedder=ib)
    progress(f"帧入库 {len(frame_list)} 张"
             + ("(含图像向量)" if ib is not None else "(图像向量未启用)"))

    # 7) 画面文字 OCR —— 双通道(设计稿 §2.5):
    #    字幕带通道拿字幕(水印在带外,天然清掉);全幅通道低频补"卖点浮层/参数文字"
    #    (字幕带裁切会把贴在画面任意位置的 `防水 10000mm` 那类文字丢掉)
    ocfg = (cfg.get("vision") or {}).get("ocr") or {}
    from memvault.config import is_off

    band_cfg = "off" if is_off(ocfg.get("band", "auto")) else "auto"
    full_every = max(1, int(ocfg.get("full_every", 5)))
    band = None
    if (ocr is not None and band_cfg != "off" and profile is not None
            and profile.subtitle_bands):
        band = profile.subtitle_bands[0]
    # 探针判出的静态文字带(水印/台标):全幅通道按坐标剔除
    watermark_bands = (profile.watermark_bands if profile else None)
    frame_ts2path = {fr["ts"]: fr["path"] for fr in frame_list}
    frame_text: dict[float, str] = {}
    ocr_texts, band_hits, full_hits = [], 0, 0
    if ocr is not None:
        for i, fr in enumerate(frame_list):
            parts = []
            if band is not None:
                band_text = ocr.read_text(fr["path"], band=band)
                if band_text:
                    band_hits += 1
                    parts.append(band_text)
                    if not units:      # 旧路径:字幕带单独成块
                        memory.add_text_chunk(
                            item_id, f"[字幕 {progress_fmt(fr['ts'])}]\n{band_text}",
                            start_ts=fr["ts"], seq=200 + i)
            if band is None or i % full_every == 0:
                full_frame_text = ocr.read_text(
                    fr["path"],
                    exclude_bands=(profile.watermark_bands if profile else None))
                if full_frame_text:
                    full_hits += 1
                    parts.append(full_frame_text)
                    if not units:      # 旧路径:全幅单独成块(与今天一致)
                        memory.add_text_chunk(
                            item_id,
                            f"[画面文字 {progress_fmt(fr['ts'])}]\n{full_frame_text}",
                            start_ts=fr["ts"], seq=300 + i)
            text = "\n".join(p for p in parts if p).strip()
            if text:
                frame_text[fr["ts"]] = text
                ocr_texts.append(text)
        mode = (f"字幕带 {band[0]*100:.0f}~{band[1]*100:.0f}%" if band else "全幅")
        progress(f"画面 OCR({mode}):{band_hits} 帧带内文字 / {full_hits} 帧全幅文字"
                 f"({reason})")
    else:
        progress(f"OCR 跳过:{reason}")

    # 8) 文本块落库
    if units:   # 单元路径:每单元一条"图文同块"(字幕 + 句子级语音 + 帧图)
        rows = []
        for i, u in enumerate(units):
            words = [w for w in (frame_text.get(u["start_frame"]),
                                 frame_text.get(u["end_frame"])) if w]
            pieces = []
            if words:
                pieces.append("字幕:" + " ".join(words))
            if u["speech"]:
                pieces.append("语音:" + "".join(u["speech"]))
            content = "\n".join(pieces).strip()
            if not content:
                continue
            rows.append({
                "content": f"[单元 {progress_fmt(u['start'])}-"
                           f"{progress_fmt(u['end'])}]\n{content}",
                "start_ts": u["start"], "end_ts": u["end"], "seq": 100 + i,
                "media_path": frame_ts2path.get(u["start_frame"]),
            })
        memory.add_text_chunks_batch(item_id, rows)
        progress(f"单元块入库 {len(rows)} 条(字幕/语音/帧图同块)")
    else:       # 旧路径:合并语音段(与今天完全一致)
        memory.add_text_chunks_batch(item_id, [
            {"content": f"[语音 {progress_fmt(seg['start'])}]\n{seg['text']}",
             "start_ts": seg["start"], "end_ts": seg["end"], "seq": 100 + i}
            for i, seg in enumerate(merged)
        ])
        progress(f"语音段合并入库 {len(merged)} 段(原始 {len(segments)} 句)")

    # 9) 汇总正文(检索兜底 + 面板预览 + AI 提取的输入)
    #    画面文字必须一起进正文:字幕视频的内容全在画面里,
    #    只放语音会让 AI 把"冰淇淋教学"总结成"广告"(真实 #14)
    asr_text = "\n".join(seg["text"] for seg in merged)
    ocr_blob = "\n".join(ocr_texts)
    full_text = "\n".join(t for t in (asr_text, ocr_blob) if t.strip()) or None
    memory.db.update_item_media(
        item_id, content_text=full_text,
        media_paths=[f["path"] for f in frame_list],
    )

    # 10a) 画像回写:该 UP 的常规形态(下次可省一次判定)
    if profile is not None and profile.ok and meta.get("up_mid"):
        try:
            from memvault.vision.frames_probe import profile_payload

            memory.db.save_frames_profile(meta["up_mid"], profile_payload(profile))
            progress(f"已缓存 UP {meta.get('up') or meta['up_mid']} 的画像")
        except Exception as e:  # noqa: BLE001 — 缓存失败不影响采集
            logger.info("画像缓存失败:%s", e)

    # 10) 弹幕广告段标注(设计稿第 5 步):观众比 UP 更早承认"这段是广告"
    dcfg = (cfg.get("vision") or {}).get("danmaku") or {}
    from memvault.config import is_off

    if meta.get("cid") and not is_off(dcfg.get("enabled", "auto")):
        try:
            from memvault.sources.bili_danmaku import mark_ad_segments

            segs = mark_ad_segments(
                memory, item_id, meta["cid"],
                cookies_path=(cfg.get("bili") or {}).get("cookies_path"),
                window=float(dcfg.get("window", 10.0)),
                min_hits=int(dcfg.get("min_hits", 2)))
            if segs:
                progress("弹幕判出广告段:" + ", ".join(
                    f"{a:.0f}~{b:.0f}s" for a, b in segs))
        except Exception as e:  # noqa: BLE001 — 弹幕拿不到不影响采集
            logger.info("弹幕广告段标注跳过:%s", e)

    # 11) 清理临时视频(帧图保留)
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
