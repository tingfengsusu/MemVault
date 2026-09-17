"""把本地书籍目录(epub/pdf/docx/...)批量解析入库,并排队 AI 分类与双链。

用法: .venv/Scripts/python scripts/ingest_books.py <目录> [--domain reading]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memvault.config import chroma_dir, db_path, load_config  # noqa: E402
from memvault.db import Database  # noqa: E402
from memvault.embeddings import get_text_embedder  # noqa: E402
from memvault.memory import Memory  # noqa: E402
from memvault.pipeline.document import DOC_EXT, parse_document  # noqa: E402
from memvault.vector_store import VectorStore  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("directory")
    ap.add_argument("--domain", default="reading")
    args = ap.parse_args()

    root = Path(args.directory)
    files = sorted(p for p in root.iterdir()
                   if p.is_file() and p.suffix.lower() in DOC_EXT)
    if not files:
        print(f"目录中没有可解析文件: {root}")
        return

    cfg = load_config()
    db = Database(db_path(cfg))
    mem = Memory(db, VectorStore(chroma_dir(cfg)), get_text_embedder(cfg))

    done = failed = 0
    for i, f in enumerate(files, 1):
        try:
            item_id = parse_document(f, mem, cfg, domain=args.domain)
            db.enqueue("auto_process", {"item_id": item_id},
                       dedup_key=f"auto|{item_id}")
            db.enqueue("build_links", {"item_id": item_id},
                       dedup_key=f"links|{item_id}")
            n = len(mem.get_item(item_id)["chunks"])
            print(f"[{i}/{len(files)}] ✓ {f.name} → #{item_id}({n} 块)")
            done += 1
        except Exception as e:  # noqa: BLE001 — 单文件失败不中断
            print(f"[{i}/{len(files)}] ✗ {f.name}: {e}")
            failed += 1
    print(f"完成:成功 {done} / 失败 {failed};AI 分类与双链已排队(启动服务后处理)")


if __name__ == "__main__":
    main()
