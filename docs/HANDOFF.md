# MemVault 项目状态与交接文档

> 用途:跨会话交接。新会话开始前先读本文件 + `DESIGN.md`(架构与设计变更史)
> + `docs/testing-log.md`(问题与解决记录),即可低成本恢复上下文。

## 一句话状态

v0.1 核心完成:多模态采集(视频/网页/商品/文档)→ 本地记忆库(SQLite+Chroma)
→ 混合检索与双链 → LLM 自动分类与提示词进化 → 聊天/订阅/京东加购技能。
测试 77/77,三分支(main / theme-notion / theme-flomo)已推送 GitHub。
当前库:155 条目 / 1600 语义块 / 201 对双链 / 279 条日志(含模拟数据)。

## 常用操作速查

```bash
# 启动(托盘常驻:面板+worker+调度+热键 Ctrl+Alt+B)
cd D:\Code\MemVault
HF_HUB_OFFLINE=1 .venv\Scripts\python -m memvault tray      # 或 run_tray.py
# 面板: http://127.0.0.1:8765   (设置页可在线切换 LLM 通道/模型/Key)

# 文档解析验收(热键不便时):复制文件路径给热键,或直接调用
.venv\Scripts\python -c "from ...pipeline.files import ingest_file; ..."

# 维护脚本
.venv\Scripts\python scripts\build_links.py        # 全库重建双链
.venv\Scripts\python scripts\check_integrity.py    # 数据体检(应 ALL_PASS)
.venv\Scripts\python scripts\gen_longterm.py       # 再造一批模拟数据
.venv\Scripts\python scripts\dedupe_categories.py  # 合并同名分类
.venv\Scripts\python -m memvault reindex           # 换嵌入模型后重建向量
```

## 环境要点(踩过的坑)

- **启动必须 `HF_HUB_OFFLINE=1`**(模型已缓存;否则 hf-mirror 不通时会卡启动几分钟)
- 服务重启较慢属正常(加载 bge 模型);不要重复启动,单实例有 health 检查保护
- B站订阅依赖 `config/cookies.txt`(登录态,已 gitignore)；`bili_ticket` 已自动续签,
  主通道被 -352 风控时自动降级 series 通道
- LLM 通道(设置页在线切换,改完即生效):
  - **api 模式(当前使用中)**:Command Code 兼容端点(`https://api.commandcode.ai/provider/v1`
    + `deepseek/deepseek-v4-flash`),走 CC 订阅额度。**注意模型是推理型**:
    思考 token 占用预算,客户端已内置"空内容自动加大预算重试";
    测试连接的预算已调至 800。切回官方:`https://api.deepseek.com/v1`。
  - **web 模式(免 token)**:Playwright 驱动 chat.deepseek.com,首次需登录一次
    (登录态存 `config/deepseek_web_auth.json`)。
- 京东加购:`JdHandler` 接管 Chrome(需要系统 Chrome + 京东登录态)
- ffmpeg 在 `tools/ffmpeg.exe`(DASH 合并需要)

## 未完成的验收(优先级最高的待办)

1. **插件采集京东商品页** + **京东加购真机点击**(商品详情页"🛒 加入京东购物车")
2. 订阅自动拉新观察一轮(30 分钟调度)
3. 文档解析真机(复制 PDF/Word/Excel/PPT + 热键)
4. 浏览器插件加载后各页面类型(选段/整页/商品/视频)实测

## 可选增强(未做)

- M5: PyInstaller 打包 + 首启向导(用户要求延后,验收完成后再做)
- 扫描版 PDF 的 OCR 兜底(EasyOCR 已装,可接)
- 检索重排序(bge-reranker)、有界 ReAct 调研技能
- 聊天流式输出(提升等待体感)
- 双链的图谱可视化(目前只有列表式相关条目)

## 关键文件地图

| 文件 | 作用 |
|---|---|
| `memvault/pipeline/video.py` | 视频管线:下载→抽帧→OCR→ASR→入库 |
| `memvault/pipeline/document.py` | 文档解析:PDF/Word/Excel/PPT |
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
