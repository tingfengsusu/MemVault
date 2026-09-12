"""MemVault 命令行(M1 验收入口)。

用法:
  python -m memvault init
  python -m memvault ingest video <B站URL或本地文件> [--domain general]
  python -m memvault query "查询词" [--domain ...] [--k 8]
  python -m memvault stats
"""
import argparse
import logging
import sys

from memvault import __version__
from memvault.config import chroma_dir, db_path, data_dir, load_config
from memvault.db import Database
from memvault.embeddings import get_text_embedder
from memvault.memory import Memory, fmt_ts
from memvault.vector_store import VectorStore


def build_memory(cfg=None) -> tuple[Database, Memory]:
    cfg = cfg or load_config()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    db = Database(db_path(cfg))
    vs = VectorStore(chroma_dir(cfg))
    emb = get_text_embedder(cfg)
    return db, Memory(db, vs, emb)


def cmd_init(_args):
    cfg = load_config()
    Database(db_path(cfg))
    print("数据目录:", data_dir(cfg))
    print("SQLite  :", db_path(cfg))
    print("向量库  :", chroma_dir(cfg))
    print("初始化完成")


def cmd_ingest(args):
    cfg = load_config()
    _, mem = build_memory(cfg)
    if args.kind == "video":
        from memvault.pipeline.video import ingest_video

        item_id = ingest_video(args.source, mem, cfg, domain=args.domain)
        print(f"OK item_id={item_id}")
    else:
        print(f"暂不支持的采集类型: {args.kind}(M2 接入网页/文档)", file=sys.stderr)
        sys.exit(2)


def cmd_query(args):
    cfg = load_config()
    _, mem = build_memory(cfg)
    results = mem.search(args.query, domain=args.domain, top_k=args.k)
    if not results:
        print("无结果")
        return
    for r in results:
        c, it = r["chunk"], r["item"]
        ts = fmt_ts(c.get("start_ts"))
        snippet = (c.get("content") or c.get("media_path") or "").replace("\n", " ")
        if len(snippet) > 80:
            snippet = snippet[:80] + "..."
        print(f"[{r['score']:.4f}] #{it['id']} {it['title']}"
              f" ({it['domain']}/{it['type']}) @ {ts}")
        print(f"          {snippet}")


def cmd_stats(_args):
    cfg = load_config()
    _, mem = build_memory(cfg)
    for k, v in mem.stats().items():
        print(f"{k:>12}: {v}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="memvault",
                                description=f"MemVault v{__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化数据目录与库").set_defaults(fn=cmd_init)

    pi = sub.add_parser("ingest", help="采集入库")
    pi.add_argument("kind", choices=["video"], help="采集类型")
    pi.add_argument("source", help="B站 URL/BV 号 或 本地文件路径")
    pi.add_argument("--domain", default="general")
    pi.set_defaults(fn=cmd_ingest)

    pq = sub.add_parser("query", help="混合检索")
    pq.add_argument("query")
    pq.add_argument("--domain")
    pq.add_argument("--k", type=int, default=8)
    pq.set_defaults(fn=cmd_query)

    sub.add_parser("stats", help="库统计").set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
