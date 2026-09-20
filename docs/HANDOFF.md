# MemVault 项目状态与交接文档

> 用途:跨会话交接。新会话开始前先读本文件 + `DESIGN.md`(架构与设计变更史)
> + `docs/testing-log.md`(问题与解决记录),即可低成本恢复上下文。
> 最近核对:**2026-09-17 14:10**(三个会话接力:12:37 数据盘点 / 13:10 缺陷修复 /
> 14:10 抽帧·OCR·UP分类·双链)

## 一句话状态

v0.1 核心完成:多模态采集(视频/网页/商品/文档)→ 本地记忆库(SQLite+Chroma)
→ 混合检索与双链 → LLM 自动分类与提示词进化 → 聊天/订阅/京东加购技能。
测试 **129/129 通过**;OCR 存档已启用(auto);双链口径重标定;图像检索(Chinese-CLIP)可用;
面板的待整理箱与检索页已迁 Vue(方案 A,双轨并存)。
三分支此前与 GitHub 一致,**本轮两次修复已提交 main、尚未推送**。
**库里现在是你自己的真实数据**(7 本 epub + 5 个 B站视频);长期模拟数据已退居备份库,
所以判断健康度**要用下面的真实数字**,不要再用旧的模拟数字。

## 当前真实数据(2026-09-17 14:10 实测)

| 指标 | 在用库 `data/memvault.db` | 备份库 `data/memvault_backup_20260917_1058.db` |
|---|---|---|
| 条目 | **12**(7 本 epub「reading」+ 5 个 B站视频) | 155(**模拟数据**) |
| 语义块 | 1465 文本 + 120 图像帧 | 1557 |
| 双链 | **8 对**(重标定后) | 200 对 |
| 分类 | 8(2 active + 6 待确认) | 16 |
| UP主规则 | 0 条(入口在条目详情页) | – |
| 日志 / 画像 / 订阅源 | 0 / 0 / **0** | 279 / – / 1 |
| FTS 行 | items_fts 12 == 条目 12;chunks_fts == 文本块(已清干净) | – |

> ⚠️ 本文件旧版写的「155 条目 / 1600 块 / 201 双链 / 279 日志」描述的是**模拟数据**,
> 那些数据现在只存在于上表右侧的备份库。真实库在 2026-09-17 11:00 前后被 epub 导入
> 与真实视频采集接管:条目 id 1–8 是书,10–13 是视频。
> **#14(冰淇淋)当天下午用新管线重跑过**:40 帧覆盖全片 + OCR 存档 40/40 帧有文字,
> 现在是"冰淇淋教程"而非"转转广告"。

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

## 第一轮修复(2026-09-17 13:10,真实数据驱动)

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

## 第二轮修复(2026-09-17 14:10:抽帧 / OCR / UP分类 / 双链)

1. **抽帧改为全片覆盖**:原实现(含 Video2Shop 原版)扫到 max_frames 就停,长视频只采
   开头几分钟。现为「全片粗扫 → 时间轴分桶 → 每桶取画面变化最大的点」,`max_frames`
   16 → **40**。真机:6 分 31 秒视频抽 40 帧覆盖 0~381s。
2. **OCR 存档启用**(`easyocr` 已装,`vision.ocr.enabled: auto`):先 ASR,人声占比 <
   30% 才跑 OCR(字幕/无配音视频),否则跳过——不做无谓的第二遍识别。参数调优到
   1.17s/帧(默认 5.5s/帧)。**画面文字会并入 `content_text`**(否则 AI 仍只看语音)。
3. **UP主 → 分类规则**:采集写入 `attrs_json.up/up_mid/bvid`;`up_categories` 表 +
   条目详情页一键绑定 + 分类页列出/解除;命中规则**不走 LLM** 直接归档。
4. **双链口径重标定**:比较文本改用 AI 摘要画像;聚合改 top-3 块均值(原"最高单块");
   排除自身块;阈值 0.55 → **0.62**(真实库分布);`set_links` 只删自己这侧的声明
   (修掉"后算的条目把先算的链接抹掉")。真实库 4 对 → **8 对**,伪链接消失。

## 第三轮(2026-09-17 傍晚:图像检索启用)

**Chinese-CLIP 已装并可用**(用户拍板):`pip install cn-clip --no-deps`(lmdb 需编译、
推理用不到),权重 `ViT-B-16` → `~/.cache/clip`(~700MB,走 hf-mirror 下载)。链路三段:
① 抽帧入库即建图像向量(`vision.image_embed.enabled: auto/off`);
② **存量/历史帧回填**用 `scripts/embed_images.py`(本次已把 104 个待索引帧补上);
③ 检索页新增「🖼 相关画面」(文字搜画面:搜"冰淇淋"出 #14 的画面墙,带时间戳)。

- 运行期注意:托盘以 `HF_HUB_OFFLINE=1` 启动,`ImageEmbedder.available()` 只认
  "已装 + 权重在本地",不会偷偷联网;缺权重时图像侧安静跳过,文本检索不受影响。
- 想重下/换模型:`HF_ENDPOINT=https://hf-mirror.com` + `load_from_name('ViT-B-16')`。

## 第四轮(2026-09-17 晚间:面板前端方案 A)

**面板开始渐进增强:待整理箱与检索页已迁到 Vue 3 + Vite,其余 7 页仍是 Jinja2。**

- **JSON 层** `memvault/server/api.py`:统一契约 `{ok,data,error}`,错误映射只对 `/api` 生效
  (旧页面路由保持原生形状,双轨并存)。读接口已铺全,动作接口按需加。
- **前端工程** `web/`:改前端后要 `cd web && npm install && npm run build` ——
  **产物直接落 `memvault/server/static/dist/`,已入库**(拉代码即可跑,不必装 node;
  但要改前端源码就得有 node,本机 node v24 / npm 走淘宝镜像)。
- **9 页全部迁完**:库首页 / 待整理箱 / 检索 / 条目详情 / 分类 / 任务 / 订阅 / 设置 / 聊天
  (挂载点 `#<page>-app`,入口 `web/src/entries/*.js`,产物 `static/dist/*.js`),
  数据走 `/api/inbox`、`/api/items`、`/api/search/images`。**回退方式 = revert 对应提交**
  (旧模板在 git 历史里)。组件复用既有 class,所以三个主题照旧生效。
- **迁移已完成**:Jinja2 只剩 `base.html`(导航 + 主题);9 个页面模板都是"挂载点 + script"。
  前端约定/加页面步骤/踩坑清单见 **`web/README.md`**;可展示证据(9 张截图 + 接口清单)
  在 **`docs/screenshots/`** 与 **`docs/api.md`**,用 `python temp/make_evidence.py` 一键刷新。
  今后加页面/改数据:接口放 `memvault/server/api.py`(统一契约 `{ok,data,error}`,
  错误映射只对 `/api` 生效),视图放 `web/src/views/` 并在 `vite.config.js` 加入口,
  改完 `cd web && npm run build`。页面断言一律改"外壳 + 接口"断言。

## 设计稿(未实现,别当成已完成)

- `docs/design-shopping-review.md` — 电商视频理解与个性化推荐设计:
  ①按规则绑定提示词(UP集 → profile)②购物复盘 schema(卖点/标签/关键片段,
  每条带时间戳证据)③偏好权重与检索重排 ④ReAct 的边界 ⑤实现顺序与验收标准。
  来源:恒洁集团 AI 产品岗 JD 第 4 条(视频理解与内容分析)。**纯文档,不动代码。**

## 抽帧/单元化的判据:设计意图 vs 现状(2026-09-20 核对)

**设计稿原意(design-frame-units.md §2.3)**:

| 画像(探针事实) | 应有策略 | 现状 |
|---|---|---|
| 人声低 + 有字幕带 | **字幕为主**:按动作事件取帧(单元化) | ✅ 已实现(单元化 + 双 OCR 通道) |
| 人声高 + 无字幕带 | **语音为主**:按变化事件取图示/图表帧 | ❌ 未实现(仍走均匀抽帧) |
| 两者都有 | 双通道,按时间配对 | ⚠️ 部分(OCR 双通道有;时间配对未做) |
| 两者都低(纯动作/音乐) | 只按动作事件取帧 | ❌ 未实现(仍走均匀抽帧) |
| 该品类有**预置配置** | 按分类/UP 的预设走(品类只选**先验**) | ❌ **未实现**——品类标签目前完全不参与抽帧决策 |

原文一句要紧的话:「品类标签(匹配系统已有分类:`up_categories` + `route_item`)只用于选
**先验**,探针事实优先。」也就是说:**分类是给"先验配置"用的,判据最终看探针**;
没有对应品类的预置配置时,就退化成"纯预处理"(画像自己决定) —— 现在**只有后一半**在跑。

**用词纠正(用户指出)**:不要叫"字幕类视频" —— 那是**通道判定的结果**(画像判「字幕为主」),
不是内容类别。界面上已改成"画像判定「字幕为主」的视频",并注明品类预置未做。

**兜底路径(均匀抽帧)为什么保留**:设计稿 §5 写明「其余保持今天 40 帧零改动」,
它是三类真实场景的退路 —— ①旁白类视频(内容在语音里,单元化无意义);
②探针判不出(无字幕带/画面全静止);③`frames.unit=off` 回退档(设计稿每步的回退方式)。
实测本库用量可在设置页看到(单元化 1 条 / 均匀 4 条)。**上限只限数量**,间隔会自动放大
保证帧铺满全片(这是更早修掉的"只采到开头"缺陷)。

**待做(按价值排序)**:①按分类/UP 的**预置配置**(把品类标签真正用作先验);
②"语音为主"与"纯动作"两个象限的策略(按变化事件取帧);③双通道的时间配对。

## 用户明确搁置的一件事(勿擅自开工)

**合集/分集**:#12「全108集」只入库了合集页第 1 个视频(采集是一视频一条目,
无合集概念)。用户答复:**暂时搁置**。

## 单 worker 串行 = "看起来卡住了"的正常来源

`worker.run_worker` 是**单线程串行**队列,一次只跑一个任务。长视频转写(ASR)期间,
后面排队的分类/双链/文档任务全都停在 pending——这是设计使然,不是故障。
(用户 09-17 曾因此以为"AI 没跑出结论",其实只是还没轮到。)

> 新增成本提示:OCR 只在 `vision.ocr.enabled=auto` 且人声占比 <30% 时触发,
> 40 帧约 1 分钟(首个视频含进程预热);普通解说视频不做第二遍识别。

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
- **提取提示词 v2**:按内容形态给属性名 + 并列对象逐个展开 + 数值保留原样;
  出厂版会在启动时自动升级(被改写过的保留,旧版可回滚)。
- **广告开关** `llm.ads_policy`:ignore(默认,广告不写进属性)/ mention(单独一条
  「推广信息」);提示词占位符 `{ads_policy}` 注入,老提示词运行期追加该策略;
  设置页有下拉,改完即生效(无需重启)。
- **前端反馈**:提示统一走右下角悬浮 `ToastHost`(不随滚动跑掉);条目详情页动作按钮
  有「⏳ 正在排队… / ✓ 已排入队列」状态,点击处立刻可见。
- **OCR 依赖已装**:`easyocr 1.7.2` + `torchvision 0.29.0`(torch 2.14.0+cpu 未变,
  即 OCR 只能跑 CPU;ASR 的 GPU 来自 CTranslate2,与 torch 无关)。
  EasyOCR 默认参数 5.5s/帧,已调成 `canvas_size=960, mag_ratio=1.0` → 1.17s/帧
  (`memvault/vision/ocr.py`)。首次调用会下载模型(约 100MB,已在
  `C:\Users\<用户>\.EasyOCR\model`)。
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
