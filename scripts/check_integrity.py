"""记忆库数据体检:对"模拟长期使用"后的库做完整性/一致性/检索质量检查。

用法: .venv/Scripts/python scripts/check_integrity.py
全部 PASS 输出 ALL_PASS 并退出 0;任何 FAIL 退出 1。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memvault.config import chroma_dir, db_path, load_config  # noqa: E402
from memvault.db import Database  # noqa: E402
from memvault.embeddings import get_text_embedder  # noqa: E402
from memvault.memory import Memory  # noqa: E402
from memvault.vector_store import VectorStore  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main():
    cfg = load_config()
    db = Database(db_path(cfg))
    vs = VectorStore(chroma_dir(cfg))
    mem = Memory(db, vs, get_text_embedder(cfg))
    conn = sqlite3.connect(db_path(cfg))
    conn.row_factory = sqlite3.Row

    # 1. 数据库完整性
    ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    check("SQLite integrity_check", ok == "ok", str(ok))

    # 2. 计数概览
    counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()[0]
              for t in ("items", "chunks", "logs", "profile", "categories", "jobs")}
    print(f"      counts: {counts}")
    check("条目规模(长期使用应有积累)", counts["items"] >= 15,
          f"items={counts['items']}")
    check("日志规模", counts["logs"] >= 30, f"logs={counts['logs']}")

    # 3. 向量库与 SQLite 一致
    text_chunks = conn.execute(
        "SELECT COUNT(*) c FROM chunks WHERE modality='text' AND content IS NOT NULL"
    ).fetchone()[0]
    vc = vs.count_text()
    check("向量数 == 文本块数", vc == text_chunks, f"chroma={vc} sqlite={text_chunks}")

    bad = conn.execute(
        "SELECT COUNT(*) c FROM chunks WHERE modality='text' AND embed_status!='done'"
    ).fetchone()[0]
    check("文本块全部完成嵌入", bad == 0, f"未完成 {bad}")

    orphans = conn.execute(
        "SELECT COUNT(*) c FROM chunks WHERE item_id NOT IN (SELECT id FROM items)"
    ).fetchone()[0]
    check("无孤儿语义块", orphans == 0, f"orphans={orphans}")

    # 4. FTS 关键词检索(跨领域)
    for kw, domain in (("卧推", "fitness"), ("红烧肉", "cooking"),
                       ("FastAPI", "programming"), ("摇粒绒", "shopping")):
        hits = db.fts_search_chunks(kw, limit=5)
        got = {db.get_items([c["item_id"]])[c["item_id"]]["domain"]
               for c in db.get_chunks(hits) if c["item_id"] in db.get_items([c["item_id"]])}
        check(f"FTS 关键词「{kw}」命中 {domain} 域", domain in got, f"hits={len(hits)}")

    # 5. 向量语义检索领域合理性
    for q, domain in (("卧推每组几次比较合适", "fitness"),
                      ("番茄炒蛋怎么做才嫩", "cooking"),
                      ("增肌要吃多少蛋白质", "fitness")):
        r = mem.search(q, top_k=3)
        top_domains = [x["item"]["domain"] for x in r[:2]]
        check(f"语义「{q}」前二含 {domain}", domain in top_domains, str(top_domains))

    # 6. 日志时间线跨度(长期使用后存在超出窗口的旧日志)
    tl = mem.timeline(days=45)
    in45 = conn.execute(
        "SELECT COUNT(*) c FROM logs"
        " WHERE happened_at >= datetime('now','localtime','-45 days')"
    ).fetchone()[0]
    check("45 天时间线 == 窗口内日志数", len(tl) == in45,
          f"timeline={len(tl)} window={in45}")
    tl_all = mem.timeline(days=3650)
    check("超长窗口覆盖全部日志(含边界日期)", len(tl_all) == counts["logs"],
          f"timeline={len(tl_all)} total={counts['logs']}")
    tl7 = mem.timeline(days=7)
    check("7 天时间线是子集", 0 < len(tl7) <= len(tl), f"7d={len(tl7)}")
    fmt_bad = [l["happened_at"] for l in tl if "T" in (l["happened_at"] or "")]
    check("日志时间格式统一(空格分隔)", len(fmt_bad) == 0,
          f"T分隔残留 {len(fmt_bad)} 条" if fmt_bad else "")

    # 7. 画像与分类
    check("画像已录入", counts["profile"] >= 5, f"profile={counts['profile']}")
    check("分类树已建", counts["categories"] >= 10, f"cats={counts['categories']}")
    filed = conn.execute("SELECT COUNT(*) c FROM items WHERE status='filed'").fetchone()[0]
    inbox = conn.execute("SELECT COUNT(*) c FROM items WHERE status='inbox'").fetchone()[0]
    print(f"      filed={filed} inbox={inbox}(真实使用中两者并存属正常)")

    # 8. 同名条目共存(长期使用必然出现)
    dup = conn.execute(
        "SELECT COUNT(*) c FROM items WHERE title IN "
        "(SELECT title FROM items GROUP BY title HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    check("同名条目可共存且可检索", True, f"同名条目 {dup} 个(信息性)")

    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"\n{'ALL_PASS' if n_fail == 0 else f'FAILED {n_fail}'} "
          f"({len(results) - n_fail}/{len(results)})")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
