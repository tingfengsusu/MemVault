"""合并同名重复分类(提议查重修复前的历史遗留数据)。

用法: .venv/Scripts/python scripts/dedupe_categories.py
规则:同 (domain, name) 多个分类时,保留 active 的(优先条目多者),
      其余分类下条目与提示词关联迁移到保留者后删除。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memvault.config import db_path, load_config  # noqa: E402


def main():
    cfg = load_config()
    conn = sqlite3.connect(db_path(cfg))
    conn.row_factory = sqlite3.Row

    groups = conn.execute(
        "SELECT domain, name, GROUP_CONCAT(id) ids FROM categories"
        " GROUP BY domain, name HAVING COUNT(*) > 1").fetchall()
    if not groups:
        print("无同名重复分类")
        return

    for g in groups:
        ids = [int(x) for x in g["ids"].split(",")]
        rows = [dict(conn.execute("SELECT * FROM categories WHERE id=?",
                                  (i,)).fetchone()) for i in ids]

        def item_count(cid):
            return conn.execute(
                "SELECT COUNT(*) c FROM items WHERE category_id=?",
                (cid,)).fetchone()[0]

        rows.sort(key=lambda r: (r["status"] != "active", -item_count(r["id"])))
        keeper = rows[0]
        for r in rows[1:]:
            n = conn.execute(
                "UPDATE items SET category_id=? WHERE category_id=?",
                (keeper["id"], r["id"])).rowcount
            conn.execute("UPDATE prompts SET category_id=? WHERE category_id=?",
                         (keeper["id"], r["id"]))
            conn.execute("DELETE FROM categories WHERE id=?", (r["id"],))
            print(f"合并 {g['domain']}/{g['name']}: "
                  f"id{r['id']}({r['status']}) → id{keeper['id']}"
                  f"({keeper['status']}),迁移 {n} 条目")
    conn.commit()
    print(f"完成:处理 {len(groups)} 组")


if __name__ == "__main__":
    main()
