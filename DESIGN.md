# 个人多模态记忆库平台 — 项目设计文档(草案 v0.1)

> 状态:**草案,待评审**。本文件暂存于 Video2Shop 仓库,新仓库建立后移入作为 `DESIGN.md`。
> 前作:[Video2Shop](https://github.com/tingfengsusu/Video2Shop)(视频→食材清单→京东加购),本平台吸收其管线作为采集源之一。

---

## 1. 项目定位

**一句话**:本地优先的个人多模态信息平台——把你在网页、视频、文档里遇到的内容"一键采集"进个人库,经过嵌入与结构化处理后,支撑检索、问答、推荐等应用;**购物决策**和**健身教练**是平台之上的两个应用技能,而非平台本身。

**设计原则**:

1. **本地优先**:所有数据(条目、画像、日志、向量)只存在本机;云端只承担模型 API 调用(DeepSeek / 嵌入模型可选本地)。
2. **平台与技能分离**:采集、存储、检索与领域无关;每个领域(购物/健身/烹饪…)只是"一种检索策略 + 一套提示词 + 可选执行动作"。
3. **三类数据一等公民**:内容条目(semantic)、个人画像(profile)、行为日志(episodic),对应 agent memory 领域的标准记忆分层。
4. **采集异步化**:热键按下立即返回"已收到",下载/转写/嵌入在后台队列慢慢做,不打断用户。
5. **复用前作**:B站下载、场景抽帧、OCR、京东 CDP 加购、DeepSeek 双模式这些已验证的代码直接移植改造。

---

## 2. 核心领域模型(存储层详细设计)

存储 = **SQLite(唯一事实源)+ 向量库(语义索引)**,双库通过 `item_id` / `chunk_id` 关联。

### 2.1 SQLite 表结构

```sql
-- 内容条目:一切采集结果的归属主体
CREATE TABLE items (
  id            INTEGER PRIMARY KEY,
  domain        TEXT NOT NULL,          -- shopping / fitness / cooking / general ...
  type          TEXT NOT NULL,          -- product / action / recipe / note / doc / video ...
  title         TEXT NOT NULL,
  attrs_json    TEXT DEFAULT '{}',      -- 领域属性:颜色尺码品牌 / 肌群强度时长 ...
  content_text  TEXT,                   -- 提取后的正文描述(文本嵌入的原料)
  media_paths   TEXT DEFAULT '[]',      -- 本地媒体文件(帧图/PDF/封面)JSON 数组
  source_type   TEXT,                   -- video / webpage / file / chat
  source_ref    TEXT,                   -- url / BV号 / 文件绝对路径
  category_id   INTEGER REFERENCES categories(id),
  status        TEXT DEFAULT 'inbox',   -- inbox(待整理) / filed(已归类) / archived
  created_at    TEXT DEFAULT (datetime('now'))
);

-- 语义块:条目内部的检索单元(一帧 / 一段转写 / 一段文档)
CREATE TABLE chunks (
  id           INTEGER PRIMARY KEY,
  item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  modality     TEXT NOT NULL,           -- text / image
  content      TEXT,                    -- 文本内容(图像块为空)
  media_path   TEXT,                    -- 图像块对应的本地帧图
  start_ts     REAL,                    -- 视频源:起始秒(溯源跳转的关键)
  end_ts       REAL,
  seq          INTEGER,                 -- 块内顺序
  embed_status TEXT DEFAULT 'pending'   -- pending / done / failed(可重跑)
);

-- 分类树:用户自定义,LLM 自动分类只能从中"选题"
CREATE TABLE categories (
  id        INTEGER PRIMARY KEY,
  domain    TEXT NOT NULL,
  parent_id INTEGER REFERENCES categories(id),
  name      TEXT NOT NULL,
  sort      INTEGER DEFAULT 0
);

-- 个人画像:关于"我"的结构化事实,对话式录入、LLM 抽取成键值
CREATE TABLE profile (
  id         INTEGER PRIMARY KEY,
  domain     TEXT DEFAULT 'general',
  key        TEXT NOT NULL,             -- 身高 / 训练目标 / 上衣尺码 ...
  value_json TEXT NOT NULL,
  source     TEXT,                      -- chat / manual
  updated_at TEXT DEFAULT (datetime('now'))
);

-- 行为日志:episodic 记忆,带发生时间,可检索时序问题
CREATE TABLE logs (
  id                 INTEGER PRIMARY KEY,
  domain             TEXT,
  happened_at        TEXT NOT NULL,
  content_text       TEXT NOT NULL,     -- "胸 5 组 + 跑步 40 分钟"
  attrs_json         TEXT DEFAULT '{}',
  related_item_ids   TEXT DEFAULT '[]', -- 关联的动作条目/商品条目
  source             TEXT               -- chat / plugin / auto
);

-- 任务队列:服务层的调度核心,同时天然完成"同源去重"
CREATE TABLE jobs (
  id          INTEGER PRIMARY KEY,
  type        TEXT NOT NULL,            -- ingest_video / ingest_webpage / ingest_file
                                        -- / embed / categorize / extract_struct
  payload     TEXT NOT NULL,            -- JSON 参数
  status      TEXT DEFAULT 'pending',   -- pending / running / done / failed
  dedup_key   TEXT UNIQUE,              -- 如 url 的 sha1,重复采集直接被拒
  priority    INTEGER DEFAULT 0,
  error       TEXT,
  created_at  TEXT DEFAULT (datetime('now')),
  started_at  TEXT, finished_at TEXT
);
```

### 2.2 向量库(Chroma)collection 设计

| collection | 嵌入模型 | 存什么 | metadata |
|---|---|---|---|
| `chunk_text` | bge-small-zh-v1.5(512维) | OCR 段、ASR 段、网页正文块、文档章节、条目描述 | `{item_id, domain, category_id, source_type, start_ts}` |
| `chunk_image` | Chinese-CLIP ViT-B/16 | 视频关键帧、PDF 图表、商品主图 | `{item_id, domain, start_ts, media_path}` |

图像与文本**分开 collection**(维度与模型都不同),检索时按需选择或双路召回。选型理由见 §10。

---

## 3. 系统架构

```
┌─────────────────────────── 触发层 ───────────────────────────┐
│  浏览器插件              托盘程序                Web 面板      │
│  选中/商品页/视频页      全局热键/文件拖放        上传/浏览/聊天 │
└──────────────┬───────────────────────────────────────────────┘
               │ HTTP 127.0.0.1:8765(仅本机)
┌──────────────▼─────────── 服务层(单进程常驻)─────────────────┐
│  FastAPI:capture / query / chat / panel 静态页                │
│  Worker 线程:轮询 jobs 表 → 分发给对应管线                     │
│  自动化 Agent:Playwright CDP(京东加购/截图,用户会话内)       │
└──────────────┬───────────────────────────────────────────────┘
┌──────────────▼─────────── 采集与处理管线 ─────────────────────┐
│ 视频:下载→场景抽帧→ASR(faster-whisper)→OCR→(LLM 结构化)    │
│ 网页:readability 正文→(截图)→LLM 摘要/商品抽取              │
│ 文档:unstructured 解析→分块→(PDF 图表抽取)                  │
│ 对话:LLM 从聊天中抽取画像键值 / 日志                          │
└──────────────┬───────────────────────────────────────────────┘
┌──────────────▼─────────── 存储层 ─────────────────────────────┐
│ SQLite(items/chunks/categories/profile/logs/jobs + FTS5)     │
│ Chroma(chunk_text / chunk_image)                             │
└──────────────┬───────────────────────────────────────────────┘
┌──────────────▼─────────── 应用层(技能)──────────────────────┐
│ Skill = 检索策略 + 提示词 + 可选执行动作                       │
│  ├─ 购物决策(执行动作 = 京东加购,移植自 Video2Shop)          │
│  └─ 健身教练(无执行动作,纯建议)                             │
└───────────────────────────────────────────────────────────────┘
```

**进程模型**:一个用户级常驻 Python 进程(pythonw 启动,任务计划程序"登录时"拉起,单实例锁防双开),内含托盘 + FastAPI + worker 线程。SQLite 开 WAL 模式支撑多线程读写。

---

## 4. 采集管线(按数据源)

### 4.1 视频(移植 + 补 ASR)

1. **下载**:复用 `bili_downloader.py`(B站 API 直连,断点续传)。
2. **抽帧**:复用 HSV 直方图场景检测(≤N 帧),帧落盘为 jpg。
3. **ASR(新增)**:faster-whisper(CPU,small 起步)→ 带时间戳的分段转写 `[{start, end, text}]`。
4. **OCR(角色变化)**:EasyOCR 结果从"过滤器"改为"存档"——每帧识别文本连同时间戳作为 text chunk 入库。
5. **入库**:1 个 item(type=video)+ 3 类 chunk(帧图/OCR段/ASR段)。
6. **结构化提取(异步、可选)**:按 domain 提示词调 LLM——食谱视频→食材清单、健身视频→动作条目,生成**子条目**(type=action/recipe)挂在该视频条目下。原始 chunk 先入库保证"未提取也能检索",LLM 挂了不丢数据。
7. 清理临时文件,chunk 进入嵌入队列。

**溯源设计**:每个 chunk 都带 `start_ts`,前端条目详情页提供"跳转到 xx:xx"链接(B站支持 `?t=秒` 定位)。

### 4.2 网页 / 商品页

- 插件上送 `{url, title, selected_text?, product?}`,服务端 readability-lxml 抽正文;正文超长时按段落分块。
- 商品页由插件直接抓结构化字段(标题/价格/主图/店铺),进 `attrs_json`,type=product,domain=shopping;主图进图像索引。
- LLM 生成一句话摘要写入 `content_text`(嵌入原料),避免把整页噪声嵌入。

### 4.3 文档(Word / Excel / PDF / PPT)

- `unstructured` 统一解析 → 按章节/工作表分块 → text chunk;PDF 内图表用 PyMuPDF 抽出 → image chunk。
- 大文件解析是慢任务,放 jobs 队列,面板显示进度。

### 4.4 对话式录入(画像与日志)

聊天界面的消息经 LLM 抽取:用户说"我身高 178,目标是增肌"→ profile 写入 `(身高, 178)`、`(训练目标, 增肌)`;用户说"今天胸 5 组 + 跑步 40 分钟"→ 写入当日 log 并关联相关动作条目。抽取失败的回落到 inbox 让用户确认,不强求全自动。

---

## 5. 嵌入与索引

| 用途 | 模型 | 说明 |
|---|---|---|
| 文本嵌入 | **BAAI/bge-small-zh-v1.5** | 中文友好、CPU 快(单机日采集量完全够);后续可平滑升级 bge-m3 |
| 图像嵌入 | **Chinese-CLIP ViT-B/16** | 中文图文对齐;torch 依赖前作已有,增量成本小 |
| 关键词 | SQLite **FTS5** | BM25 全文,与向量做混合召回 |
| 重排(可选,M4+) | bge-reranker-base | 混合召回后精排 |

嵌入由 worker 异步完成,失败可重跑(`embed_status`),模型可后期替换(重跑全量嵌入即可)。

---

## 6. 记忆 API(检索层)

对上层(技能/面板)只暴露一个 `memory` 模块,参考 Mem0 的 API 形态:

```python
memory.search(query, domain=None, category_id=None, type=None,
              time_range=None, top_k=8)
    # 混合召回:向量 topK + FTS5 关键词,RRF 融合;返回 chunk + 所属条目摘要

memory.get_profile(domain=None, keys=None)          # 画像读取
memory.add_log(domain, content, happened_at=None, related_items=[])
memory.timeline(domain, days=7)                     # 时序日志(健身教练的主力查询)
memory.get_item(item_id)                            # 详情 + chunks(含时间戳)
```

技能侧的典型组装(以健身教练为例):

```
用户输入: "今天胸 5 组,力量 60kg,有点胸闷"
context = timeline(fitness, days=7)          # 近一周训练日志
        + get_profile('fitness')             # 身体数据与目标
        + search("卧推 胸部训练 动作要领")     # 库里的动作条目/视频片段
reply  = LLM(教练提示词, context, 用户输入)   # "上周三刚练过肩…明天建议下肢…"
log    = add_log(...)                        # 本次对话自动落日志
```

---

## 7. 应用技能层

### 7.1 技能接口

```python
class Skill(Protocol):
    domain: str                                   # shopping / fitness
    def build_context(self, query: str) -> Context # 调 memory.* 组装检索结果
    def reply(self, ctx: Context, user_input: str) -> Reply
    def act(self, reply: Reply) -> ActionResult | None   # 可选:执行动作
```

新增一个领域 = 写一个 Skill 类 + 一组分类树 + 提示词,平台零改动。

### 7.2 购物决策技能(移植自 Video2Shop)

- **入库方向**:商品页插件采集 → product 条目入"个人库"(愿望清单语义)。
- **决策方向**:浏览商品时,插件把当前商品发给技能 → `search(同类目)` 找已收藏/已拥有 → LLM 给出"已有类似款 / 重复收藏提醒 / 可搭配建议"。
- **执行动作**:移植 `JdHandler`(Playwright CDP 加购),但升级为:先向量匹配商品库候选,再精确搜索,替代原"名称直搜点第一个"的盲匹配。

### 7.3 健身教练技能

- 健身视频入库 → LLM 抽动作条目(动作名/目标肌群/强度/时长)→ 与日常日志、画像一起构成教练上下文(见 §6 示例)。
- 领域知识(恢复时间、训练分化常识)由 LLM 自身提供;**库只负责"关于我个人的上下文"**——这是采集范围的边界:关于我的、我感兴趣的才入库。

---

## 8. 触发层与常驻形态

### 8.1 浏览器插件(Chrome MV3)

- content script:取选中文本、当前 URL、标题;商品站点按站点规则抓 `{title, price, image, shop}`;B站/YouTube 页面识别视频 ID 与当前播放时间戳。
- background service worker:`POST http://127.0.0.1:8765/api/capture`,成功后角标提示"已入库(后台处理中)"。
- 定期 `GET /api/health`,服务不在线时角标变灰(避免用户按了没反应)。
- 插件做得很薄:不做下载、不做解析,全部交给本地服务。

**采集协议(核心接口)**:

```
POST /api/capture
{ "type": "selection" | "page" | "product" | "video",
  "url": "...", "title": "...",
  "text": "选中文本(可选)",
  "product": { "name": "...", "price": "...", "image_url": "..." },   // 商品页
  "video":  { "platform": "bilibili", "id": "BV...", "t": 123.4 } }    // 视频页
→ { "job_id": 42, "status": "accepted" }
```

### 8.2 托盘程序与全局热键

- pystray 托盘:打开面板 / 待整理箱(N 条)/ 暂停采集 / 退出。
- `keyboard` 库注册全局热键,默认 `Ctrl+Alt+B`:读剪贴板(资源管理器选中文件 Ctrl+C 即得路径 → 文件采集;网页复制文本 → 文本采集)。
- Windows 通知(plyer)反馈任务完成/失败。

### 8.3 为什么不是标准 Windows 服务

Windows 服务运行在 **Session 0**:无桌面、无 UI、**无法驱动用户会话里的 Chrome**——而京东加购与截图都依赖接管浏览器。因此采用"**登录自启的托盘常驻**"形态:用户级权限,自动化能力完整,免服务安装/卸载的管理成本。未来若需要多用户/无头部署,再把管线部分拆成真服务,与用户会话代理用 jobs 表通信。

---

## 9. 界面(Web 面板,仅本机)

轻量前端:FastAPI + Jinja2 模板 + HTMX(不引重框架)。

| 页面 | 内容 |
|---|---|
| 库浏览 | 按 domain/分类树浏览条目卡片;条目详情含媒体预览、时间戳跳转、来源链接 |
| 待整理箱 | status=inbox 的条目,拖拽归类/改名/补属性 |
| 聊天 | 顶部选技能(购物/健身…),对话式问答与录入 |
| 任务 | jobs 队列状态(处理中/失败重试) |
| 设置 | 分类树管理、模型与 API key、热键配置 |

---

## 10. 技术选型总表

| 层 | 选型 | 理由 / 备注 |
|---|---|---|
| 语言 | Python 3.10+ | 前作代码直接移植 |
| Web/API | FastAPI + uvicorn | Pydantic 定义采集协议,比 Flask 更适合 API+异步;替代前作 Flask |
| 常驻 | pythonw + pystray + keyboard + 任务计划程序 | 见 §8.3,不用标准服务 |
| 队列 | SQLite jobs 表 + worker 轮询 | 单机规模不上 Celery/Redis |
| 存储 | SQLite(WAL + FTS5) | 事实源,零运维 |
| 向量库 | **Chroma**(备选 Milvus Lite) | pip 即用、持久化、metadata 过滤;Milvus Lite 叙事更强但依赖稍重 |
| ASR | faster-whisper(small 起) | CPU 可跑、带时间戳;补前作最大缺口 |
| OCR | EasyOCR(复用) | 从过滤器升级为存档源 |
| 视觉 | OpenCV(复用)+ Chinese-CLIP | 抽帧复用,图像嵌入新增 |
| 文档解析 | unstructured + PyMuPDF | Word/Excel/PPT/PDF 统一处理 |
| 网页解析 | readability-lxml + Playwright | 正文抽取 + 截图 |
| 自动化 | Playwright CDP(复用 JdHandler) | 京东加购、网页截图 |
| LLM | DeepSeek API(双模式可移植) | 结构化提取/分类/对话;后期可加 Ollama 本地兜底 |
| 打包 | PyInstaller onedir(沿用前作方案) | 面板与插件分发文档化,安装包后置到 M5 |

---

## 11. 分期路线图与验收标准

| 期 | 内容 | 验收标准 |
|---|---|---|
| **M1 平台地基** | 项目骨架、SQLite 全部建表、视频管线(下载/抽帧/OCR/**ASR**)、Chroma 入库、memory.search 最小版、CLI 验证脚本 | 一条命令把一个 B站视频完整入库,并能检索出"某句话出现在 xx:xx" |
| **M2 触发层** | 托盘常驻 + 热键 + jobs 队列、浏览器插件(网页/视频/商品)、Web 面板(浏览/待整理) | 刷视频时按热键,30 秒内条目进库并出现在面板 |
| **M3 记忆与分类** | 分类树管理、LLM 自动分类(inbox 兜底)、对话式画像/日志录入、memory API 全量 | 健身视频+书籍入库,对话录入个人情况后,"我最近练过什么"能答对 |
| **M4 应用技能** | 健身教练技能、购物决策技能(含 JdHandler 移植与向量匹配升级) | 输入今日训练得个性化建议;商品页一键入库,浏览同类目出推荐 |
| **M5 打磨发布** | 重排、推荐质量调优、PyInstaller 打包、文档与安装器 | 交付安装包,新用户 10 分钟内跑通首次采集 |

每期独立可演示;M1 完成即覆盖面试题"多模态采集 + 向量库",M4 完成即覆盖"平台抽象 + 执行层"的完整故事。

---

## 12. 开放问题(需要拍板)

1. **向量库**:Chroma(我推荐,起步快)vs Milvus Lite(面试叙事强)?
2. **LLM 策略**:全走 DeepSeek API(简单,有成本)vs 加 Ollama 本地兜底(隐私好,占资源)?M1 先全 API,是否需要本地兜底?
3. **视频源**:M1 仅 B站(复用现有下载器);YouTube 是否要支持(yt-dlp 一行接入,但网络环境要求)?
4. **面板技术**:Jinja2+HTMX(我推荐,轻)vs Vue/React 单页?
5. **项目名与仓库可见性**:待定(影响仓库创建)。

---

## 13. v0.2 变更(2026-09 评审讨论并入)

> 本文件已迁入 MemVault 仓库作为正式设计文档。以下章节来自设计评审讨论,与正文冲突处以本节为准。

### 13.1 分类与提示词进化系统(M3)

核心决策:**提示词不硬编码,存 DB 带版本**(`prompts` / `prompt_feedback` 表已建)。

- **流程 A · 日常采集(高频,单角色,保持便宜)**:路由(LLM 从用户分类树"选择题" + 向量检索 3 条最相似已分类条目做参考)→ 按分类调用专属提取提示词。confidence < 0.8 进待整理箱。
- **流程 B · 新分类创建(低频,多角色)**:Proposer 提议 → Writer 生成提取提示词(含输出 schema)→ Critic 用样例自检 → draft,用户确认一次后转 active。
- **流程 C · 质疑驱动进化(低频,多角色)**:用户对结果提出质疑 → 向量检索该分类下相似历史质疑(feedback 向量索引)→ Analyzer 分析考量维度 → Rewriter 基于当前提示词+出错样例+历史教训产出新版本 → Self-check 用回归集对比新旧 → 应用。
- **三个护栏**:①分类树锚定 + 提议确认制(防分类爆炸);②提示词版本化 + diff + 回滚(防越改越差);③每个分类保留多源混合的"用户认可结果"回归集。
- **提示词绑定"提取目标与 schema",不绑定数据源**:同一分类下视频/图片帖/文档共用一条提示词;源相关字段(如弹幕舆情)在 schema 中标可选,缺失模态输出 null,提取输入按条目实际 chunk 组装,后校验拒绝引用不存在的模态。
- 面试术语:human-in-the-loop prompt optimization;相关参考 DSPy / Reflexion / TextGrad。编排先用纯 Python,M4 可将流程 B/C 迁移 LangGraph(有循环与条件分支的场景)。

### 13.2 订阅采集(M3)

- `watch_sources` 表(kind: UP主/关键词/RSS;target;domain;last_checked)+ 定时线程轮询 → 有新内容即生成 capture job 进现有管线,`dedup_key` 天然去重。
- "自动"的边界:**对用户**,关注一个新源是设置页点一下,零代码;**对开发者**,接入一个全新平台需要写适配器。此级任务路径可预知,不需要 ReAct。

### 13.3 有界 ReAct 调研技能(M4/M5 可选)

- ReAct = LLM 在循环中"推理 → 调工具 → 观察 → 再推理",适用于**任务路径不可预知**的开放任务(如"帮我调研冬季通勤穿搭并入库")。
- 约束:工具白名单(搜索/入库/检索记忆/总结)+ 最大步数 + 批量入库前用户确认。
- 实现先手写 while 循环(工具注册表 + LLM 决策 + 结果回填),框架(LangGraph)按需后置。

### 13.4 M1 落地记录

SQLite 九张表已建(§2.1 六张 + prompts + prompt_feedback + watch_sources);混合检索(向量 + FTS5-trigram,RRF 融合)、视频管线(B站下载 → 带时间戳场景抽帧 → OCR 存档 → faster-whisper ASR → 入库)、记忆 API、CLI(`init/ingest/query/stats`)、worker 轮询循环均已实现,详见 README。

### 13.5 M2 落地记录(触发层)

- **服务层** `memvault/server/app.py`:FastAPI(127.0.0.1:8765),`POST /api/capture` 按 §8.1 协议接收 selection/page/product/video 四类采集,sha1 去重键防重复入库;`/api/health` 供插件探活;`/api/pause` 暂停开关;静态挂载 `/media` 提供帧图缩略图。
- **面板**:Jinja2 + 原生 CSS(无外部依赖)。页面:库浏览(统计+最近条目)/ 混合搜索 / 待整理箱(状态流转 inbox→filed/archived)/ 条目详情(语义块时间轴、B站 `?t=` 定位跳转)/ 任务队列。worker 提供 `step()` 单步执行,面板/测试均可手动驱动。
- **常驻形态** `memvault/tray.py`:pystray 托盘(打开面板/待整理计数/暂停切换/退出)+ uvicorn 线程 + worker 线程 + `keyboard` 全局热键(默认 Ctrl+Alt+B,读剪贴板:文件路径→文件任务、URL→视频/链接任务、文本→摘录任务)+ 单实例检查(health 探测,已运行则直接开面板)。`python -m memvault autostart on` 用 schtasks 注册登录自启(pythonw 无窗口)。
- **浏览器插件** `extension/`(MV3):点击图标 → scripting.executeScript 注入 gatherPage(自包含函数)自动判定页面类型(B站视频页带播放时间/京东/淘宝商品页/选中文字/整页正文前 8000 字)→ POST capture → 角标 ✓/!/× 反馈;30s 探活,服务离线灰显。
- **新增处理器** `pipeline/text.py`(ingest_text/ingest_product)、`pipeline/files.py`(ingest_file:视频→视频管线,图片→复制入数据目录,其他→占位待 M3 文档解析;capture_from_clipboard 热键入口)。
- 测试 24 例(API 端到端/面板渲染/去重/暂停/校验/B站跳转链接)。

### 13.6 M3a 落地记录(自动分类 + 提示词进化)

- **LLM 客户端** `memvault/llm.py`:OpenAI 兼容(DeepSeek),key 解析顺序 config → 环境变量 → .env → 复用 Video2Shop 配置(仅本机);未配置时 enabled=False,所有调用方必须走降级路径。
- **路由** `classify.route_item`:分类树"选择题" + 向量检索 3 条相似已分类条目作参考 → JSON {category_id/new_category, confidence, reason}。conf ≥ 0.8 且分类 active → item 转 filed;提议新分类 → categories 建状态=proposed 的行,条目留待整理箱,面板确认后生效;低置信 → 留箱并写 auto_note。
- **提取** `classify.extract_item`:按分类取活跃提示词(无则用通用版),输出 JSON 合并进 attrs_json(null 字段不落库)。
- **提示词系统** `memvault/prompts.py`:版本化(new_version 自动退役旧 active、rollback 回滚)、种子 router/extract 提示词、add_feedback(质疑入库 + 向量化进 feedback collection,metadata 带 category_id)、similar_critiques(同分类语义召回历史质疑)、rewrite_from_feedback(无专属版先从通用分叉 v1 → LLM 基于当前提示词+出错样例+历史质疑改写 → 新版本 origin=feedback)。
- **接入**:采集成功自动排队 auto_process(带去重键);面板新增分类管理页(增分类/确认提议)、条目详情显示分类与置信度 + "重新分析"按钮;分类未确认前不参与路由选择题。
- 测试 34 例(路由三分支/提取合并/版本回滚/质疑改写/分叉/LLM 未配置跳过/自动排队/面板分类流)。DeepSeek 连通性已真机验证。

### 13.7 M3b 落地记录(订阅采集 + 对话式录入)

- **订阅** `memvault/scheduler.py` + `sources/bili_watch.py`:watch_sources 三类操作(添加含 UP主昵称解析/启停/立即检查);调度线程按时间桶给启用源排 watch_check 任务(去重),worker 执行。适配器:wbi 签名 + buvid 预热 + dm 指纹参数;**B站对匿名访问投稿列表已全面风控(-352/-799),实测 spi/ExClimbWuzhi/legacy 接口均不可用,最终方案为用户登录 cookies.txt**(config.bili.cookies_path,与下载器共用),错误信息带配置指引。首次检查只登记历史(seed 成 done 任务),此后增量。
- **聊天** `memvault/chat.py`:一轮 = LLM 抽取(profile 键值 / log 事件 / search_query)→ 持久化画像与日志 → memory.search 组装个人上下文 → 按技能(general/fitness/shopping)提示词生成回复。面板 /chat 页消息不落盘,抽取结果落盘;LLM 未配置返回 503。
- **托盘/serve** 均已启动调度线程;测试 45 例(wbi 确定性/UID 解析/首查登记/增量去重/时间桶/聊天抽取持久化/面板流)。

### 13.8 M4 落地记录(应用技能 + 执行层)

- **技能化** `memvault/skills/builtin.py`:SkillSpec = 提示词 + 日志过滤(domain/天数) + 检索过滤(search_kwargs)。general/fitness/shopping 三个内置技能;fitness/shopping 检索限定自身领域,通用技能不过滤;chat.py 改为按技能取检索策略与提示词,新增领域只加一个 SkillSpec。
- **执行层** `memvault/automation/`:JdHandler 整体移植自 Video2Shop(690 行,零内部依赖,Playwright CDP 接管 Chrome:搜索"{关键词} 自营"→点第一结果加购→重试与确认)。安全边界:**加购仅由用户在面板商品详情页点击"🛒 加入京东购物车"触发**,入 jd_cart 队列由 worker 执行,LLM/对话永远不能自动下单;失败(含京东未登录 5 分钟超时)在任务页可见。
- 测试 51 例(技能注册/领域过滤/通用不过滤/加购成功与失败/端点与模板)。
