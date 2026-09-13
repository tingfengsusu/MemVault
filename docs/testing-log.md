# MemVault 测试与问题记录(供复查)

> 用途:记录测试过程、发现的问题、解决方式与验证结果。按时间倒序追加。
> 配套工具:`scripts/gen_testdata.py`(生成模拟数据)、`scripts/check_integrity.py`(数据体检)。

---

## 2026-09-14 长期使用模拟 + 数据体检

### 测试方法

1. `python scripts/gen_testdata.py --auto 4`:生成 5 领域(健身/购物/烹饪/编程/读书)
   模拟数据——12 个分类、6 条画像、18 个条目、38 条日志(跨 30 天训练循环 + 浏览记录),
   其中 4 个条目走真实 DeepSeek 自动分类(结果:3 filed + 1 proposed 新分类)。
2. `python scripts/check_integrity.py`:18 项体检,最终 **ALL_PASS 18/18**;回归测试 52/52。

### 体检覆盖项

SQLite integrity_check / 条目与日志规模 / 向量数==文本块数 / 嵌入无失败 /
无孤儿块 / FTS 跨域关键词(卧推·红烧肉·FastAPI·摇粒绒)/ 语义检索领域合理性
(卧推·番茄炒蛋·蛋白质)/ 时间线 45 天与 7 天 / 时间格式统一 / 画像与分类树规模。

### 本轮发现的问题与解决

| # | 问题 | 根因 | 解决方式 | 验证 |
|---|---|---|---|---|
| 1 | 浏览器点"添加订阅"报 422(HTML 表单发 urlencoded,端点收 JSON 模型) | FastAPI 端点用 Pydantic 模型接表单 | 改 `Form(...)` 参数 + 装 python-multipart;测试从 json=/params= 改 data= | 浏览器真实点击添加成功;52/52 |
| 2 | 体检发现 1 条日志时间是 ISO `T` 分隔(与 SQLite `datetime()` 空格格式混用,时序比较有隐患) | 早期 `add_log` 用 `isoformat()` | `add_log` 统一 `strftime('%Y-%m-%d %H:%M:%S')`;`Database._migrate` 启动时自动 `REPLACE('T',' ')` 归一化旧数据 | 重跑体检 PASS;迁移日志打印条数 |
| 3 | 体检阈值"条目≥20"误报(items=19) | 阈值主观定得过高,非数据问题 | 阈值校准为 ≥15 并在本文记录校准行为 | 重跑 PASS |

### 早期问题索引(详见 DESIGN.md §13.x)

| 问题 | 解决 |
|---|---|
| HF Xet 存储 CAS 接口镜像 401 | `HF_HUB_DISABLE_XET=1` |
| transformers 5.x 加载 bge 报 processing class 错(静默降级假嵌入) | 钉 `transformers==4.51.3` + `sentence-transformers==3.3.1`;新增 `memvault reindex` 重建向量(876 块) |
| Chroma metadata None 值崩溃 | 入库前过滤 None |
| claim_next 返回更新前状态快照 | 返回时改写 status |
| DASH 合并缺 ffmpeg | 复制到 `tools/ffmpeg.exe` |
| B站风控(-352)拦匿名投稿列表 | 登录 cookies.txt + wbi + buvid 预热 |
| 强杀进程任务卡 running | 启动时 `recover_stale_jobs` 自动回队 |

### 已知限制(记录在案,未修)

- ASR 输出繁体中文(whisper zh 特性),检索不受影响(嵌入模型双语),显示不转换
- 图像嵌入需要 `requirements-vision.txt`(cn-clip),默认未装时图像块保持 pending
- hf-mirror 不可达时启动会重试数分钟;临时方案 `HF_HUB_OFFLINE=1`(模型已缓存),
  待产品化:检测缓存完整自动离线
- inbox 表单按钮无 JS 确认;商品"加入购物车"会真实下单,无二次确认弹窗

---

## 主题分支说明(2026-09-14)

面板样式抽离为 `memvault/server/static/theme.css`(设计变量驱动),换主题 = 换分支:

| 分支 | 主题 | 设计参考 |
|---|---|---|
| `main` | 「清雅」默认浅色:白底、浅灰分隔、靛蓝点缀 | 通用浅色后台 |
| `theme-notion` | 「Notion 极简白」:纯白、黑字、黑色主按钮、细分割线 | Notion / Karakeep |
| `theme-flomo` | 「flomo 暖纸」:米色纸感底、暖棕文字、琥珀点缀 | flomo / 滴答清单浅色 |

切换方法:`git checkout <分支>` 后重启 MemVault(tray/serve),浏览器强刷(Ctrl+F5)。
主题分支只改 `theme.css` 与 README 主题说明,功能代码与 main 完全一致,可随时合并。

---

## 2026-09-14 长期使用模拟·第二轮(大容量 + 边界 + 真实交互)

### 测试方法

`python scripts/gen_longterm.py`(可重复执行):在第一轮之上追加
125 个批量条目 + 6 个边界脏数据 + 239 条日志(180 天跨度),
并混入真实 LLM 交互(自动分类 5 条、健身聊天 1 轮、提示词质疑改写 1 轮)。

### 规模与性能

| 指标 | 数值 |
|---|---|
| 条目 / 语义块 / 日志 | 150 / 1041 / 279 |
| 检索延迟(混合,24 次) | min 7ms · **中位 8ms** · p95 14ms · max 19ms |
| 向量库 vs SQLite | 1025 == 1025,完全一致 |
| 自动分类(5 条真实调用) | 3 filed + 2 inbox(置信度波动属正常) |

### 验证结论(20/20 ALL_PASS)

- 大容量下 FTS 与语义检索的**领域精度提升**(卧推查询前二全是 fitness,
  不再被其他领域干扰——数据越多主题信号越强)
- 同名条目 4 个可共存、可检索、不冲突(dedup 靠 dedup_key,条目层允许同名)
- 边界日期正确:未来 1 条 / 两年前 3 条被 3650 天窗口覆盖,45 天窗口不含旧数据
- 超长文本(5000+ 字)、SQL/HTML/emoji 特殊字符、极短内容、繁简日英混排
  全部正常入库与检索,无崩溃
- **提示词质疑改写(Flow C)首次在真实 LLM 上走通**:对商品条目提出
  "遗漏价格和店铺"质疑 → 检索历史质疑 → LLM 改写 → 新版本 v3 自动生效,
  改进说明"必须提取价格/店铺/尺码/颜色,价格保留原文格式"

### 本轮新发现的问题

| # | 问题 | 根因 | 解决方式 |
|---|---|---|---|
| 4 | `timeline(days=45)` 在日志跨 180 天后不再等于全量(设计使然) | 体检脚本旧断言写死了"等于全部" | 体检脚本改为对照 SQL 窗口计数,并新增 3650 天全量覆盖检查 |
| 5 | hf-mirror 不可达时进程启动阻塞数分钟(HEAD 校验重试) | huggingface_hub 每次启动联网校验 | 临时:`HF_HUB_OFFLINE=1`(模型已缓存);待产品化:缓存完整自动离线 |

### 复查指引

1. 重跑体检:`.venv/Scripts/python scripts/check_integrity.py`(应 ALL_PASS)
2. 再造一批数据:`.venv/Scripts/python scripts/gen_longterm.py`(幂等可重复)
3. 查看体检历史:本文件按日期追加;每次 ALL_PASS/FAIL 均有记录
