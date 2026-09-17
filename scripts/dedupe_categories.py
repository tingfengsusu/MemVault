"""合并重复分类(提议查重修复前的历史遗留数据)。

用法: .venv/Scripts/python scripts/dedupe_categories.py
规则:同领域内"完全同名"或"仅差一个泛化词尾"(如 影视解读/影视解读范畴)的分类
      视为同一桶;保留 active 的(同级再取名字最短者,即最干净的名字),
      其余分类下条目与提示词关联迁移到保留者后删除。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memvault.classify import same_category_name  # noqa: E402
from memvault.config import db_path, load_config  # noqa: E402


def cluster(rows: list[dict]) -> list[list[dict]]:
    """把同领域内互为变体的分类聚成组。"""
    groups: list[list[dict]] = []
    for r in rows:
        for g in groups:
            if g[0]["domain"] == r["domain"] and any(
                    same_category_name(m["name"], r["name"]) for m in g):
                g.append(r)
                break
        else:
            groups.append([r])
    return [g for g in groups if len(g) > 1]


def main():
    cfg = load_config()
    conn = sqlite3.connect(db_path(cfg))
    conn.row_factory = sqlite3.Row

    def item_count(cid):
        return conn.execute("SELECT COUNT(*) FROM items WHERE category_id=?",
                            (cid,)).fetchone()[0]

    rows = [dict(r) for r in conn.execute("SELECT * FROM categories ORDER BY id")]
    groups = cluster(rows)
    if not groups:
        print("无重复分类")
        return

    total = 0
    for g in groups:
        # 保留 active;同级取名字最短(最干净),再看条目数
        g.sort(key=lambda r: (r["status"] != "active", len(r["name"]),
                              -item_count(r["id"])))
        keeper = g[0]
        for r in g[1:]:
            n = conn.execute("UPDATE items SET category_id=? WHERE category_id=?",
                             (keeper["id"], r["id"])).rowcount
            conn.execute("UPDATE prompts SET category_id=? WHERE category_id=?",
                         (keeper["id"], r["id"]))
            conn.execute("DELETE FROM categories WHERE id=?", (r["id"],))
            total += 1
            print(f"合并 {g[0]['domain']}/{r['name']}(id{r['id']}, {r['status']})"
                  f" → {keeper['name']}(id{keeper['id']}, {keeper['status']})"
                  f",迁移 {n} 条目")
    conn.commit()
    print(f"完成:合并 {total} 个分类")


if __name__ == "__main__":
    main()
