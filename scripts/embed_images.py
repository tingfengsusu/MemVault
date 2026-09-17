"""把库里"待索引"的图像块补上 Chinese-CLIP 向量(装完权重后跑一次)。

用法:
    HF_ENDPOINT=https://hf-mirror.com .venv/Scripts/python scripts/embed_images.py
    # 权重已在本地时不需要 HF_ENDPOINT(托盘用 HF_HUB_OFFLINE=1 也能跑)

场景:抽帧时没装 cn_clip(或权重没下好),图像块会停在 embed_status='pending';
装好后跑本脚本补索引,之后"文字搜画面 / 以图搜图"就能命中这些帧。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memvault.cli import build_memory  # noqa: E402
from memvault.config import load_config  # noqa: E402


def main():
    cfg = load_config()
    db, memory = build_memory(cfg)
    ib = memory.image_embedder
    if ib is None:
        print("Chinese-CLIP 不可用:请先 `pip install cn-clip` 并下载权重")
        print("  权重下载(走镜像):HF_ENDPOINT=https://hf-mirror.com "
              ".venv/Scripts/python -c \"from cn_clip.clip import load_from_name;"
              " load_from_name('ViT-B-16', device='cpu')\"")
        return 1

    rows = db._conn().execute(
        "SELECT id, item_id, media_path FROM chunks"
        " WHERE modality='image' AND embed_status='pending'"
        " AND media_path IS NOT NULL ORDER BY id").fetchall()
    if not rows:
        print("没有待索引的图像块")
        return 0
    print(f"待索引图像块 {len(rows)} 个,开始编码(CPU 上约 0.3~1s/张)...")

    done = skipped = 0
    batch = 8
    for i in range(0, len(rows), batch):
        group = rows[i:i + batch]
        paths = [r["media_path"] for r in group]
        vecs = ib.encode_image_batch(paths, batch_size=batch)
        ids, good_vecs, metas = [], [], []
        for r, v in zip(group, vecs):
            if v is None or not Path(r["media_path"]).is_file():
                skipped += 1
                continue
            item = db.get_items([r["item_id"]]).get(r["item_id"]) or {}
            ids.append(r["id"])
            good_vecs.append(v)
            metas.append({k: val for k, val in {
                "item_id": r["item_id"],
                "domain": item.get("domain"),
                "media_path": str(r["media_path"]),
            }.items() if val is not None})
        if ids:
            memory.vs.upsert_image(ids, good_vecs, metas)
            marks = ",".join("?" * len(ids))
            db._conn().execute(
                f"UPDATE chunks SET embed_status='done' WHERE id IN ({marks})",
                ids)
            db._conn().commit()
            done += len(ids)
        print(f"  {i + len(group)}/{len(rows)} 已索引 {done},跳过 {skipped}")

    print(f"完成:索引 {done} 个图像块(跳过 {skipped});"
          f"图像集合共 {memory.vs.count_image()} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
