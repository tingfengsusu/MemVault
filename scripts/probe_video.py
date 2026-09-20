"""视频画像探针 CLI(设计稿第 0 步的验证入口)。

用法:
    .venv/Scripts/python scripts/probe_video.py temp/videos/xxx.mp4
    .venv/Scripts/python scripts/probe_video.py <视频> --expect-band 0.50 0.54

打印画像 JSON,并在给了 --expect-band 时对比判定是否落在期望带内
(设计稿对 #14 的期望:字幕带 50~54%、事件 ~76 个、中位时长 2~3s)。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--hz", type=float, default=5.0)
    ap.add_argument("--text-frames", type=int, default=10)
    ap.add_argument("--expect-band", type=float, nargs=2, default=None,
                    help="期望字幕带(归一化 y0 y1),给了就做对比")
    ap.add_argument("--expect-events", type=int, default=None)
    args = ap.parse_args()

    from memvault.vision.frames_probe import probe_video

    prof = probe_video(args.video, hz=args.hz, text_frames=args.text_frames)
    print(json.dumps(prof.summary(), ensure_ascii=False, indent=2))

    if args.expect_band:
        want = tuple(args.expect_band)
        ok = any(abs(b[0] - want[0]) < 0.05 and abs(b[1] - want[1]) < 0.05
                 for b in prof.subtitle_bands)
        print(f"\n期望字幕带 {want} → {'✓ 命中' if ok else '✗ 未命中'}")
    if args.expect_events:
        delta = abs(prof.event_count - args.expect_events)
        print(f"事件数 {prof.event_count}(期望 {args.expect_events}±10%)→ "
              f"{'✓' if delta <= args.expect_events * 0.1 else '✗ 偏差 ' + str(delta)}")
    return 0 if prof.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
