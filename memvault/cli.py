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


def cmd_reindex(_args):
    """用当前嵌入模型重建全部文本向量(换模型/修复降级后用)。"""
    cfg = load_config()
    _, mem = build_memory(cfg)
    n = mem.reindex_text(progress=lambda m: print(f"\r{m}", end="", flush=True))
    print(f"\n重嵌入完成: {n} 个文本块")


def cmd_serve(_args):
    """启动 API + 面板(无托盘,适合服务器/调试)。"""
    import uvicorn

    from memvault.server.app import create_app
    from memvault.worker import recover_stale_jobs

    cfg = load_config()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    recover_stale_jobs(Database(db_path(cfg)))
    s = cfg["server"]
    uvicorn.run(create_app(cfg, start_worker=True),
                host=s["host"], port=s["port"], log_level="info")


def cmd_tray(_args):
    """托盘常驻(推荐日常形态):面板 + worker + 全局热键。"""
    from memvault.tray import run

    run()


def cmd_autostart(args):
    """注册/取消 Windows 登录自启(计划任务,无窗口 pythonw)。"""
    import subprocess
    import sys
    from pathlib import Path

    if sys.platform != "win32":
        print("仅支持 Windows", file=sys.stderr)
        sys.exit(2)
    from memvault.config import PROJECT_ROOT

    if args.action == "on":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.exists():
            pythonw = Path(sys.executable)
        tr = f'"{pythonw}" "{PROJECT_ROOT / "run_tray.py"}"'
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", "MemVault", "/TR", tr,
             "/SC", "ONLOGON", "/F"],
            capture_output=True, text=True)
    else:
        r = subprocess.run(["schtasks", "/Delete", "/TN", "MemVault", "/F"],
                           capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.strip() or ("成功" if r.returncode == 0 else f"失败 code={r.returncode}"))
    sys.exit(r.returncode)


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

    sub.add_parser("reindex", help="重建全部文本向量").set_defaults(fn=cmd_reindex)

    ps = sub.add_parser("serve", help="启动 API+面板(无托盘)")
    ps.set_defaults(fn=cmd_serve)

    pt = sub.add_parser("tray", help="托盘常驻(面板+worker+热键)")
    pt.set_defaults(fn=cmd_tray)

    pa = sub.add_parser("autostart", help="登录自启(计划任务)")
    pa.add_argument("action", choices=["on", "off"])
    pa.set_defaults(fn=cmd_autostart)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
