"""模拟长期使用的多领域测试数据生成器。

用法:
  .venv/Scripts/python scripts/gen_testdata.py            # 生成数据(不调 LLM)
  .venv/Scripts/python scripts/gen_testdata.py --auto 5   # 前 5 个条目走真实自动分类

模拟场景:健身(30 天训练日志+画像)/ 购物(商品收藏+浏览记录)/
烹饪(菜谱)/ 编程笔记 / 读书笔记,共约 28 条目、45 天日志跨度。
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memvault.config import chroma_dir, db_path, load_config  # noqa: E402
from memvault.db import Database  # noqa: E402
from memvault.embeddings import get_text_embedder  # noqa: E402
from memvault.memory import Memory  # noqa: E402
from memvault.vector_store import VectorStore  # noqa: E402


def d(days_ago, hm="10:00:00"):
    """days_ago 天前的 'YYYY-MM-DD HH:MM:SS'。"""
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d ") + hm


CATS = {
    "fitness": ["胸部训练", "背部训练", "腿部训练", "有氧"],
    "shopping": ["服装", "数码", "家居"],
    "cooking": ["家常菜", "烘焙"],
    "programming": ["后端", "工具"],
    "reading": ["读书笔记"],
}

PROFILE = [
    ("fitness", "身高", "178cm"), ("fitness", "体重", "68kg"),
    ("fitness", "训练目标", "增肌,每周4练"), ("fitness", "伤病史", "左膝深蹲有旧伤,负重慎"),
    ("general", "上衣尺码", "L"), ("general", "裤长", "32"),
]

ITEMS = [
    # (domain, type, title, content, attrs)
    ("fitness", "note", "卧推力量提升要点", "平板卧推增肌要点:肩胛骨后收下沉,杠铃轨迹落到乳头连线;5x5 计划适合新手力量期;组间休息 90-180 秒;每周胸训练容量 10-20 组。", None),
    ("fitness", "note", "深蹲膝盖保护", "深蹲膝盖内扣是膝伤主因;膝盖与脚尖方向一致; boxes squat 箱式深蹲对膝伤恢复友好;热身做蚌式开合激活臀中肌。", None),
    ("fitness", "note", "引体向上进阶路线", "引体向上 0→10 进阶:先做离心(跳上慢慢下)3x5;弹力带辅助;每周两次背训;配合划船平衡推拉。", None),
    ("fitness", "note", "增肌蛋白质补充", "增肌期蛋白质 1.6-2.2g/kg 体重;鸡胸肉每 100g 约 24g 蛋白;训练后 30 分钟内补充 30-40g 蛋白+快碳;乳清蛋白方便但正餐优先。", None),
    ("shopping", "product", "优衣库摇粒绒外套 429107", "摇粒绒外套,秋冬保暖,价格 199 元,颜色藏青/米白,尺码 S-XXL", {"price": "199元", "shop": "京东"}),
    ("shopping", "product", "Nike Pegasus 41 跑鞋", "飞马 41 缓震跑鞋,适合日常慢跑,价格 899 元,尺码偏小半码", {"price": "899元", "shop": "京东"}),
    ("shopping", "product", "小米手环 9", "手环 9,心率/睡眠监测,16 天续航,价格 249 元", {"price": "249元", "shop": "小米商城"}),
    ("shopping", "product", "宜家特提亚工作台灯", "LED 工作台灯,三档色温,价格 129 元", {"price": "129元", "shop": "宜家"}),
    ("shopping", "product", "无牛津纺衬衫", "免烫牛津纺衬衫,商务休闲,价格 299 元,白色/浅蓝", {"price": "299元", "shop": "京东"}),
    ("cooking", "note", "红烧肉做法", "红烧肉:五花肉 500g 切块焯水;冰糖炒糖色;加生抽两勺老抽一勺料酒;小火炖 40 分钟;大火收汁。肥而不腻关键在焯水后煸炒出油。", None),
    ("cooking", "note", "番茄炒蛋技巧", "番茄炒蛋:蛋液中加几滴料酒和一点点水更嫩;番茄去皮口感好;先炒蛋盛出再炒番茄;最后混合加糖提鲜。", None),
    ("cooking", "note", "清蒸鲈鱼火候", "清蒸鲈鱼:水开后再上锅,大火 8 分钟关火再焖 2 分钟;铺葱姜,淋蒸鱼豉油,热油激香。", None),
    ("programming", "note", "FastAPI 依赖注入模式", "FastAPI Depends 支持嵌套依赖和 yield 清理;数据库会话用 yield 生成器注入;测试时 app.dependency_overrides 替换依赖。", None),
    ("programming", "note", "SQLite WAL 模式注意点", "WAL 模式下读写不互斥但仍是单写者;检查点(checkpoint)把 WAL 合并回主库;备份要连 .db/.wal/.shm 三个文件或用 VACUUM INTO。", None),
    ("programming", "note", "Chroma metadata 过滤", "Chroma where 条件支持 $eq/$in/$gt;None 值不能进 metadata 会报错;多条件传 dict 默认 AND。", None),
    ("programming", "note", "Playwright 等待策略", "Playwright 不要用 sleep;wait_for_load_state('networkidle') 有时不触发;优先 wait_for_selector 定位业务元素;locator 自带自动等待。", None),
    ("reading", "note", "《原子习惯》摘录", "习惯四定律:让它显而易见/有吸引力/简便易行/令人愉悦;身份认同驱动习惯——不是跑步,而是成为跑步者;两分钟规则降低启动成本。", None),
    ("reading", "note", "《卡片笔记写作法》要点", "卢曼卡片盒: fleeting notes → literature notes → permanent notes;用自己的话重写才算理解;卡片之间建立链接产生复利。", None),
]

LOGS_FITNESS = [  # 30 天训练循环
    ("胸:平板卧推 5x8 60kg + 上斜哑铃 4x10", 1), ("背:引体 4x6 + 划船 4x10", 2),
    ("腿:深蹲 5x5 80kg + 罗马尼亚硬拉 3x10", 3), ("休息日,散步 30 分钟", 4),
    ("胸:卧推 5x5 65kg + 飞鸟 3x12", 5), ("背:高位下拉 4x10 + 直臂下压 3x12", 6),
    ("腿:腿举 4x12 + 箭步蹲 3x10", 7), ("跑步 30 分钟", 8),
    ("胸:卧推 5x8 62.5kg + 双杠臂屈伸 3x10", 9), ("背:引体 4x7 + 划船 4x12", 10),
    ("腿:深蹲 5x5 82.5kg", 11), ("休息", 12),
    ("胸:上斜卧推 4x10 + 绳索夹胸 3x15", 13), ("背:杠铃划船 4x8 60kg", 14),
    ("腿:深蹲 3x5 85kg + 腿弯举 3x12", 15), ("跑步 40 分钟", 16),
    ("胸:卧推 5x6 65kg", 17), ("背:引体 5x6 + 面拉 3x15", 18),
    ("腿:腿举 4x15 + 提踵 4x20", 19), ("休息", 20),
    ("胸:哑铃卧推 4x12 30kg", 21), ("背:高位下拉 4x12 + 划船 4x10", 22),
    ("腿:深蹲 5x5 85kg 达成", 23), ("跑步 25 分钟", 24),
    ("胸:卧推 5x8 67.5kg", 25), ("背:引体 4x8", 26),
    ("腿:深蹲 3x3 90kg", 27), ("游泳 40 分钟", 28),
    ("胸:卧推 5x5 70kg + 飞鸟 3x12", 29), ("背:划船 4x10 + 直臂下压 3x12", 30),
]
LOGS_OTHER = [
    ("浏览京东:看了优衣库摇粒绒外套和 Nike 跑鞋", 2, "shopping"),
    ("浏览淘宝:无印良品衬衫", 4, "shopping"),
    ("把小米手环 9 加入了收藏", 6, "shopping"),
    ("比价:同款台灯拼多多便宜 20 元", 7, "shopping"),
    ("下单了摇粒绒外套", 8, "shopping"),
    ("整理书签,把算法文章归档", 3, "general"),
    ("周末大扫除", 9, "general"),
    ("看了 MemVault 相关的 RAG 文章", 5, "programming"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", type=int, default=0,
                    help="对前 N 个条目执行真实 LLM 自动分类提取")
    args = ap.parse_args()

    cfg = load_config()
    db = Database(db_path(cfg))
    vs = VectorStore(chroma_dir(cfg))
    mem = Memory(db, vs, get_text_embedder(cfg))

    n_cat = 0
    for domain, names in CATS.items():
        for name in names:
            db.add_category(domain, name)
            n_cat += 1

    for domain, key, value in PROFILE:
        mem.set_profile(key, value, domain=domain, source="seed")

    item_ids = []
    for domain, type_, title, content, attrs in ITEMS:
        iid = mem.add_item(domain, type_, title, attrs=attrs,
                           content_text=content, source_type="webpage",
                           source_ref=f"https://example.com/{domain}/{title}")
        mem.add_text_chunk(iid, content)
        item_ids.append((iid, domain))

    for content, days_ago in LOGS_FITNESS:
        mem.add_log(content, domain="fitness", happened_at=d(days_ago, "21:30:00"),
                    source="seed")
    for content, days_ago, domain in LOGS_OTHER:
        mem.add_log(content, domain=domain, happened_at=d(days_ago, "20:00:00"),
                    source="seed")

    print(f"生成完成: 分类 {n_cat} | 画像 {len(PROFILE)} | 条目 {len(ITEMS)} | "
          f"日志 {len(LOGS_FITNESS) + len(LOGS_OTHER)}")

    if args.auto > 0:
        from memvault.llm import LLMClient
        from memvault.pipeline.auto import auto_process

        llm = LLMClient(cfg)
        print(f"对前 {min(args.auto, len(item_ids))} 个条目执行自动分类...")
        for iid, _ in item_ids[:args.auto]:
            r = auto_process({"item_id": iid}, mem, cfg, llm=llm)
            print(f"  item#{iid}: {r.get('action')}")


if __name__ == "__main__":
    main()
