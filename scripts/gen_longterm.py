"""长期使用压力模拟(第二轮):大容量 + 边界用例 + 真实 LLM 交互。

在第一轮基础上追加:
  - ~125 个新条目(5 领域,批量嵌入)
  - ~250 条日志跨 180 天(含未来 1 条、两年前 3 条的边界数据)
  - 脏数据边界:同名条目/超长内容/特殊字符/重复内容/繁简混排
  - 真实 LLM 交互:自动分类 5 条、健身聊天 1 轮、提示词质疑改写 1 轮
  - 检索延迟基准(向量/混合各 20 次)

用法: .venv/Scripts/python scripts/gen_longterm.py
"""
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memvault.chat import chat_turn  # noqa: E402
from memvault.config import chroma_dir, db_path, load_config  # noqa: E402
from memvault.db import Database  # noqa: E402
from memvault.embeddings import get_text_embedder  # noqa: E402
from memvault.llm import LLMClient  # noqa: E402
from memvault.memory import Memory  # noqa: E402
from memvault.pipeline.auto import auto_process  # noqa: E402
from memvault.prompts import PromptStore  # noqa: E402
from memvault.vector_store import VectorStore  # noqa: E402

random.seed(42)

# ── 内容模板(领域 × 主题池,组合出大批量"真实感"条目)──────────────
POOLS = {
    "fitness": [
        ("卧推{0}计划第{1}周", "第{1}周卧推安排:周一平板 5x{0}kg,周四上斜 4x10;辅助绳索夹胸 3x15,注意肩胛稳定。"),
        ("背部训练笔记{1}", "引体向上 {0} 组,配合高位下拉;杠铃划船重量 {0}kg;组间休息两分钟,专注背阔肌收缩。"),
        ("腿部日记录{1}", "深蹲 {0}kg 5x5,腿举 4x12;左膝有旧伤,深蹲前做蚌式激活,幅度控制在大腿平行地面。"),
        ("有氧与恢复{1}", "跑步 {0} 分钟,心率控制在 150 以下;泡沫轴放松股四头肌和小腿;睡眠 7 小时以上。"),
    ],
    "shopping": [
        ("{0}元好物分享第{1}期", "本期好物:{0}元预算内的实用小物;做工和售后是重点;附购买链接与优惠信息。"),
        ("数码产品{1}评测摘录", "续航 {0} 小时,重量 {0}0g;屏幕素质中规中矩;同价位竞品对比后性价比一般。"),
        ("服装尺码记录{1}", "试穿记录:上衣 L 合身,裤长 32;{0} 分评价;面料含棉量高,洗后不缩水。"),
    ],
    "cooking": [
        ("家常菜谱{1}:{0}的做法", "主料 {0};步骤:焯水去腥→热油爆香→大火快炒→小火收汁;调味只用盐糖生抽;火候是关键。"),
        ("烘焙笔记{1}", "烤箱预热 180 度 {0} 分钟;面粉与黄油比例 2:1;中途不要开炉门;出炉后晾网冷却。"),
    ],
    "programming": [
        ("技术笔记{1}:{0}实践", "{0} 的实践记录:配置要点、常见坑、性能对比;附可运行的最小示例与参考链接。"),
        ("源码阅读{1}", "阅读 {0} 源码:核心抽象是事件循环;错误处理层层包装;文档滞后于代码,以测试为准。"),
    ],
    "reading": [
        ("《{0}》读书摘录{1}", "核心观点:复利来自持续的小改进;方法轮:目标→系统→反馈;金句摘抄与个人批注。"),
        ("书评{1}:{0}", "这本书讲 {0};优点是案例翔实;缺点是翻译生硬;适合速读,精读价值一般。"),
    ],
}

EDGE_ITEMS = [
    # (domain, type, title, content, 说明)
    ("general", "note", "超长文档测试:完整的部署手册", "第一章 环境准备。\n" + "1. 安装依赖并验证版本;检查端口占用;配置环境变量;记录变更。" * 120,
     "5000+ 字符长文本,考验分块与嵌入"),
    ("shopping", "product", "优衣库摇粒绒外套 429107", "与第一轮完全同名的重复商品,检验同名条目共存与检索区分度。",
     "与既有条目同名"),
    ("general", "note", "特殊字符('\";<drop>--)标题", "内容包含引号'\"、分号;、SQL 片段 '; DROP TABLE items;--、HTML <b>标签</b>、emoji 🧠📚、换行\n与制表符\t。",
     "SQL/HTML/emoji 特殊字符"),
    ("fitness", "note", "深蹲膝盖保护", "与既有条目完全同名的重复深蹲笔记,第二份内容更简短。", "跨轮同名"),
    ("programming", "note", "繁簡混排與日本語テスト", "這是繁體中文的技術筆記,混在簡體庫中;日本語のテキストも含む;Mixed with English tokens. 驗證跨字形檢索。",
     "繁简日英混排"),
    ("general", "note", "x", "短", "极短标题与内容"),
]

SUBJECTS = {
    "fitness": ["力量", "耐力", "柔韧", "爆发"],
    "shopping": ["百元", "千元", "平价", "旗舰"],
    "cooking": ["红烧", "清蒸", "快炒", "炖煮"],
    "programming": ["Python", "数据库", "前端", "运维"],
    "reading": ["习惯", "思维", "历史", "小说"],
}


def bulk_items(mem):
    n = 0
    for domain, templates in POOLS.items():
        subjects = SUBJECTS[domain]
        for i in range(25):  # 每领域 25 条 → 125 条
            t = templates[i % len(templates)]
            subject = subjects[i % len(subjects)]
            seq = i // len(templates) + 1
            title = t[0].format(subject, seq) + f" #{i:03d}"
            content = t[1].format(random.randint(3, 90), seq) + \
                f" 备注:本条为长期使用模拟数据,领域 {domain},序号 {i:03d}。"
            iid = mem.add_item(domain, "note", title, content_text=content,
                               source_type="webpage",
                               source_ref=f"https://example.com/lt/{domain}/{i:03d}")
            mem.add_text_chunk(iid, content)
            n += 1
    return n


def edge_items(mem):
    n = 0
    for domain, type_, title, content, note in EDGE_ITEMS:
        iid = mem.add_item(domain, type_, title, content_text=content[:6000],
                           source_type="webpage",
                           source_ref=f"https://example.com/edge/{n}")
        mem.add_text_chunk(iid, content[:6000])
        n += 1
    return n


def bulk_logs(mem):
    """180 天健身循环 + 各领域杂项 + 边界日期。"""
    parts = ["胸:卧推", "背:引体", "腿:深蹲", "肩:推举", "休息"]
    n = 0
    base = datetime.now() - timedelta(days=180)
    for i in range(180):  # 每 2 天一练 → 90 条
        dt = (base + timedelta(days=i * 2)).strftime("%Y-%m-%d %H:%M:%S")
        p = parts[i % len(parts)]
        content = f"{p} {'5x5 ' + str(60 + i // 10 * 2) + 'kg' if p != '休息' else '完全休息日'}"
        mem.add_log(content, domain="fitness", happened_at=dt, source="seed")
        n += 1
    for i in range(40):  # 购物浏览
        dt = (datetime.now() - timedelta(days=i * 3 + 1)).strftime("%Y-%m-%d %H:%M:%S")
        mem.add_log(f"浏览商品第 {i} 期,比价并收藏", domain="shopping",
                    happened_at=dt, source="seed")
        n += 1
    for i in range(15):  # 读书
        dt = (datetime.now() - timedelta(days=i * 7 + 2)).strftime("%Y-%m-%d %H:%M:%S")
        mem.add_log(f"读书 40 分钟,摘录 {i} 则", domain="reading",
                    happened_at=dt, source="seed")
        n += 1
    # 边界:未来 1 条、两年前 3 条
    mem.add_log("时钟超前测试:这条日志来自未来", domain="general",
                happened_at=(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                source="seed")
    for i in range(3):
        mem.add_log(f"陈年旧日志 {i}(两年前)", domain="general",
                    happened_at=(datetime.now() - timedelta(days=730 - i)).strftime("%Y-%m-%d %H:%M:%S"),
                    source="seed")
        n += 1
    n += 1
    return n


def bench(mem):
    """检索延迟基准:纯语义 20 次常复用查询 + 混合。"""
    queries = ["卧推每组几次", "红烧肉怎么做", "跑步心率控制", "Python 依赖注入",
               "读书方法摘录", "外套尺码选择"]
    lat = []
    for q in queries:
        for _ in range(4):
            t0 = time.perf_counter()
            mem.search(q, top_k=8)
            lat.append((time.perf_counter() - t0) * 1000)
    lat.sort()
    print(f"  检索延迟: min={lat[0]:.0f}ms 中位={lat[len(lat)//2]:.0f}ms "
          f"p95={lat[int(len(lat)*.95)]:.0f}ms max={lat[-1]:.0f}ms (n={len(lat)})")


def main():
    cfg = load_config()
    db = Database(db_path(cfg))
    vs = VectorStore(chroma_dir(cfg))
    mem = Memory(db, vs, get_text_embedder(cfg))
    llm = LLMClient(cfg)
    ps = PromptStore(db)

    print("== 1. 批量条目 ==")
    n = bulk_items(mem)
    print(f"  新增条目 {n}")

    print("== 2. 边界脏数据 ==")
    n = edge_items(mem)
    print(f"  新增边界条目 {n}")

    print("== 3. 批量日志(180 天跨度 + 边界日期)==")
    n = bulk_logs(mem)
    print(f"  新增日志 {n}")

    if llm.enabled:
        print("== 4. 真实 LLM:自动分类 5 条 ==")
        rows = db._conn().execute(
            "SELECT id FROM items ORDER BY id DESC LIMIT 40").fetchall()
        picked = [r["id"] for r in rows[::8]][:5]
        for iid in picked:
            r = auto_process({"item_id": iid}, mem, cfg, llm=llm)
            print(f"  item#{iid}: {r.get('action')}")

        print("== 5. 真实 LLM:健身聊天 1 轮 ==")
        r = chat_turn(mem, llm, "这周练了胸背腿各两次,有点疲劳,周末怎么安排?",
                      skill="fitness")
        print(f"  reply: {r['reply'][:60]}... | 画像+{len(r['profile_saved'])} 日志+{1 if r['log_saved'] else 0}")

        print("== 6. 真实 LLM:提示词质疑改写 1 轮 ==")
        row = db._conn().execute(
            "SELECT id FROM items WHERE type='product' LIMIT 1").fetchone()
        if row:
            item = mem.get_item(row["id"])
            r = ps.rewrite_from_feedback(
                mem, llm, "extract", "extract", None, item,
                "提取时遗漏了商品的价格和店铺信息,请确保价格保留原格式")
            print(f"  新提示词版本 id={r['prompt_id']} | {r['version_note'][:50]}")
    else:
        print("== LLM 未配置,跳过真实交互 ==")

    print("== 7. 检索延迟基准 ==")
    bench(mem)

    stats = mem.stats()
    print(f"== 完成 == 当前库:{stats}")


if __name__ == "__main__":
    main()
