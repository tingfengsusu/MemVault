"""为全库条目重建双链(backfill / 数据导入后执行一次)。

用法: .venv/Scripts/python scripts/build_links.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memvault.config import chroma_dir, db_path, load_config  # noqa: E402
from memvault.db import Database  # noqa: E402
from memvault.embeddings import get_text_embedder  # noqa: E402
from memvault.links import rebuild_all  # noqa: E402
from memvault.memory import Memory  # noqa: E402
from memvault.vector_store import VectorStore  # noqa: E402


def main():
    cfg = load_config()
    db = Database(db_path(cfg))
    mem = Memory(db, VectorStore(chroma_dir(cfg)), get_text_embedder(cfg))
    print(f"开始重建双链(阈值 {cfg['links']['similarity_threshold']})...")
    result = rebuild_all(mem, cfg, progress=print)
    n_pairs = db._conn().execute("SELECT COUNT(*) c FROM links").fetchone()["c"]
    print(f"完成: 扫描 {result['items']} 条,建立 {n_pairs} 对链接")


if __name__ == "__main__":
    main()
