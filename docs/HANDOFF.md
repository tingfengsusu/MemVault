# MemVault 项目状态与交接文档

> 用途:跨会话交接。新会话开始前先读本文件 + `DESIGN.md`(架构与设计变更史)
> + `docs/testing-log.md`(问题与解决记录),即可低成本恢复上下文。
> 最近核对:**2026-09-17 13:10**;文档由两个会话接力维护(12:37 版 + 13:10 版修复)

## 一句话状态

v0.1 核心完成:多模态采集(视频/网页/商品/文档)→ 本地记忆库(SQLite+Chroma)
→ 混合检索与双链 → LLM 自动分类与提示词进化 → 聊天/订阅/京东加购技能。
测试 **91/91 通过**;三分支此前与 GitHub 一致,**本轮修复已提交 main、尚未推送**。
**库里现在是你自己的真实数据**(7 本 epub + 5 个 B站视频);长期模拟数据已退居备份库,
所以判断健康度**要用下面的真实数字**,不要再用旧的模拟数字。

## 当前真实数据(2026-09-17 13:10 实测)

| 指标 | 在用库 `data/memvault.db` | 备份库 `data/memvault_backup_20260917_1058.db` |
|---|---|---|
| 条目 | **12**(7 本 epub「reading」+ 5 个 B站视频) | 155(**模拟数据**) |
| 语义块 | 1431 文本 + 80 图像帧 | 1557 |
| 双链 | 2 对 | 200 对 |
| 分类 | 7(2 active + 5 待确认) | 16 |
| 日志 / 画像 / 订阅源 | 0 / 0 / **0** | 279 / – / 1 |
| FTS 行 | items_fts 12 == 条目 12;chunks_fts 1431 == 文本块 1431(已清干净) | – |

> ⚠️ 本文件旧版写的「155 条目 / 1600 块 / 201 双链 / 279 日志」描述的是**模拟数据**,
> 那些数据现在只存在于上表右侧的备份库。真实库在 2026-09-17 11:00 前后被 epub 导入
> 与真实视频采集接管:条目 id 1–8 是书,10–13 是视频。

## 数据体检的正确用法(先看这条,免得误判)

`scripts/check_integrity.py` 的用例是按**模拟数据画像**写死的(「卧推/红烧肉/FastAPI/摇粒绒」
必须命中 fitness/cooking/programming/shopping 域,还有条目规模、日志数、画像、16 个分类、
45 天时间线),所以在真实库上必然输出 `FAILED 12 (8/20)`。
这是**脚本预期不匹配,不是数据损坏**:

- 结构类断言在真实库上**全部 PASS**:SQLite `integrity_check=ok`、向量数==文本块数、
  无孤儿语义块、嵌入全部完成、时间线格式统一;
- 想要「全绿」,把备份库复制成临时库再跑该脚本;
- 可改进(低优先):给它加 `--profile sim|real` 开关。

## 已知问题(已定位,今天不影响使用)

> ✅ 12:37 版列的两条 FTS 问题**已在 13:10 修复**(见下节):`items_fts` 幽灵/重复行
> 由启动清理处理(现 12 行 == 12 条目),删除条目时同步清 `chunks_fts`/`items_fts`。
> `items_fts` 仍是"只写不读"的冗余表(无任何查询使用),要下线可另开一次改动。

## 本轮会话修复(2026-09-17 13:10,真实数据驱动)

1. **长文提取覆盖**:此前 `extract_item` 截前 3000 字,#11(16468 字)只覆盖 18%、
   #13(11329 字)26% → 属性只反映开头,用户体感"没有结论总结"。现改为
   「开头 40% + 中段均匀抽样 + 结尾整段」;真实 LLM 复验:#11 新增「核心结论/
   风险与2026展望」、#13 新增「结论/关键案例」。
2. **分类提议**:①「属于X类」这类弱措辞也能解析;②显式点名(结构化字段或"新建/归入")
   不再受置信度限制,弱措辞仍守 0.5;③"仅差一个泛化词尾"的名字视为同一分类
   (「影视解读」==「影视解读范畴」),历史重复 #7 已并入 #4。
3. **推理模型预算**:`chat_json` 2000→4000→8000 逐级升(真实 #13 曾在 4000 上失败)。
4. **删条目对称清理**:作废指向它的排队任务(避免"条目不存在"失败)+ 清 FTS 行;
   `auto_process` 遇已删条目静默跳过。

> 数据侧只动过:合并重复分类(#7→#4)、#13/#14 重跑 AI 属性。**未删除任何条目**。
> 用户明确搁置的两件事(见下)本轮只诊断、未实现。

## 用户明确搁置的两件事(勿擅自开工)

1. **字幕无配音视频**:#14 画面有字幕(已用系统 OCR 证实:「所有水果熬入」「最后一款
   凤梨加香蕉」),但配音只有 36 秒转转口播(VAD 滤掉 5:55),于是被总结成广告。
   修法是启用 OCR 存档(DESIGN §4.1 已设计,`easyocr` 未安装)+ 抽帧加密。
   用户答复:**暂时不做**。
2. **合集/分集**:#12「全108集」只入库了合集页第 1 个视频(采集是一视频一条目,
   无合集概念)。用户答复:**暂时搁置**。

## 单 worker 串行 = "看起来卡住了"的正常来源

`worker.run_worker` 是**单线程串行**队列,一次只跑一个任务。长视频转写(ASR)期间,
后面排队的分类/双链/文档任务全都停在 pending——这是设计使然,不是故障。
核对当时:**running 1 + pending 7**(一个 44 分钟视频正在转写,后面压着 3 组 auto_process/build_links)。
ASR 一结束会一次性补跑,双链数量也会随之从 1 对涨上去。

## 常用操作速查

```bash
# 启动(托盘常驻:面板+worker+调度+热键 Ctrl+Alt+B)
#  ⚠️ 必须独立成一次工具调用、不接任何原生管道,否则会话会永久挂起(见 AGENTS.md)
cd D:\Code\MemVault
HF_HUB_OFFLINE=1 .venv\Scripts\python -m memvault tray      # 或 run_tray.py
# 面板: http://127.0.0.1:8765   (设置页可在线切换 LLM 通道/模型/Key)

# 文档入库:正常流程是「剪贴板放文件本身或文件路径文本 → 按 Ctrl+Alt+B」
# 热键不便时的等效命令(本次已冒烟验证接线正确):
.venv\Scripts\python -c "from memvault.config import load_config; from memvault.cli import build_memory; from memvault.pipeline.files import ingest_file; cfg=load_config(); db,m=build_memory(cfg); print(ingest_file({'path': r'D:\某文件.pdf'}, m, cfg))"

# 维护脚本
.venv\Scripts\python scripts\build_links.py        # 全库重建双链
.venv\Scripts\python scripts\check_integrity.py    # 数据体检(注意上面的"模拟画像"限制)
.venv\Scripts\python scripts\gen_longterm.py       # 再造一批模拟数据
.venv\Scripts\python scripts\dedupe_categories.py  # 合并同名分类
.venv\Scripts\python -m memvault stats              # 库统计
.venv\Scripts\python -m memvault reindex            # 换嵌入模型后重建向量

# 注意:CLI 的 ingest 只支持 video;文档/网页走热键
# (capture API 支持 type = selection|page|product|video,没有 file 类型)
```

## 环境要点(踩过的坑)

- **启动必须 `HF_HUB_OFFLINE=1`**(模型已缓存;否则 hf-mirror 不通时会卡启动几分钟)
- 服务重启较慢属正常(加载 bge 模型);不要重复启动,单实例有 health 检查保护
- B站订阅依赖 `config/cookies.txt`(登录态,已 gitignore);`bili_ticket` 已自动续签,
  主通道被 -352 风控时自动降级 series 通道
- LLM 通道(设置页在线切换,改完即生效):
  - **api 模式(当前使用中)**:Command Code 兼容端点(`https://api.commandcode.ai/provider/v1`
    + `deepseek/deepseek-v4-flash`),走 CC 订阅额度。**注意模型是推理型**:
    思考 token 占用预算,客户端已内置"空内容自动加大预算重试";测试连接预算已调至 800。
    切回官方:`https://api.deepseek.com/v1`。
  - **web 模式(免 token)**:Playwright 驱动 chat.deepseek.com,首次需登录一次
    (登录态存 `config/deepseek_web_auth.json`)。
- 京东加购:`JdHandler` 接管 Chrome(需要系统 Chrome + 京东登录态)
- ffmpeg 在 `tools/ffmpeg.exe`(DASH 合并需要)
- 双链目前只有 1 对属正常:双链是随条目入库逐条计算的,想让全网立刻铺满,
  手动跑一次 `scripts/build_links.py` 即可

## 未完成的验收(优先级最高的待办)

| # | 验收项 | 怎么点 / 注意 |
|---|---|---|
| 1 | 插件采集京东商品页 | 打开任意京东商品页 → 点插件图标 → 看「库」页是否出现带价格/店铺的卡片 |
| 2 | 京东加购 | 随便找个便宜商品的条目 → 详情页「🛒 加入京东购物车」→ 任务页看结果(会真的操作 Chrome) |
| 3 | 文档解析真机 | 剪贴板放 PDF/Word/Excel → Ctrl+Alt+B → 库页应出现带 📄 的条目 |
| 4 | 订阅自动拉新 | **被阻塞**:在用库里 `watch_sources = 0`(唯一那条订阅源在备份库里)→ 先去「订阅」页加一个 UP 主,再点「立即检查」 |
| 5 | 聊天走新 API | 聊天页发一句,确认切到 CC 端点后对话正常 |

> 提醒:1–3、5 都需要 worker 有档期。若正有长视频在转写,任务会停在 pending,
> 先看「任务」页确认队列空了再点,免得把它误判成失败。

## 可选增强(未做)

- M5: PyInstaller 打包 + 首启向导(用户要求延后,真机验收完成后再做)
- 扫描版 PDF 的 OCR 兜底(链路已在,`easyocr` 未安装;视频抽帧会打印
  `easyocr 未安装,OCR 存档跳过`,属预期)
- 检索重排序(bge-reranker)、有界 ReAct 调研技能
- 聊天流式输出(提升等待体感)
- 双链的图谱可视化(目前只有列表式相关条目)
- 下线 `items_fts`(只写不读的冗余表;幽灵行已清,表本身还在写)

## 关键文件地图

| 文件 | 作用 |
|---|---|
| `memvault/pipeline/video.py` | 视频管线:下载→抽帧→OCR→ASR→入库 |
| `memvault/pipeline/document.py` | 文档解析:PDF/Word/Excel/PPT |
| `memvault/pipeline/files.py` | 热键入口:剪贴板 → 文件/链接/文本任务 |
| `memvault/worker.py` | 单线程任务循环 + 中断恢复 |
| `memvault/classify.py` | 路由(分类树+置信度三分支)、提取、提议查重 |
| `memvault/prompts.py` | 提示词版本化 + 质疑进化(Flow C) |
| `memvault/links.py` | 双链:向量相似度→相关条目 |
| `memvault/llm.py` / `llm_web.py` | LLM 通道工厂(API/网页双后端) |
| `memvault/server/` | FastAPI 面板(模板+静态主题层) |
| `memvault/automation/jd.py` | 京东加购(移植自 Video2Shop) |
| `memvault/sources/bili_watch.py` | B站订阅(wbi+票据续签+降级通道) |

## 新会话开场提示词模板

```
读 D:\Code\MemVault\docs\HANDOFF.md、DESIGN.md 与 docs/testing-log.md,
继续 MemVault 项目。当前需求:<你的具体需求>
```
