# MemVault

本地优先的**个人多模态记忆库**:把你在视频、网页、文档里遇到的内容一键采集进个人库,经抽帧/OCR/ASR/嵌入后存入 SQLite + 向量库,支撑混合检索、溯源跳转与个性化应用(购物决策、健身教练……)。

> 设计文档见 [DESIGN.md](DESIGN.md)。前作 [Video2Shop](https://github.com/tingfengsusu/Video2Shop)(视频→食材清单→京东加购)的管线已吸收为采集源之一。

## 当前状态:M3a(自动分类 + 提示词进化)

- ✅ M1 平台地基:SQLite 九张表(FTS5 中文全文)、Chroma、记忆 API(混合检索 RRF)、视频管线(B站下载/带时间戳抽帧/OCR 存档/ASR)、CLI、worker
- ✅ M2 触发层:FastAPI 服务(127.0.0.1:8765)+ `/api/capture` 采集协议、Web 面板、托盘常驻(热键 Ctrl+Alt+B)、Chrome 插件
- ✅ M3a:LLM 自动分类(路由→按分类提示词提取属性;低置信度/提议新分类→待整理箱与待确认分类)、提示词版本化 + 质疑驱动进化(feedback 向量库召回相似历史质疑)、分类管理页、条目"重新分析"
- ⏳ M3b:订阅采集(watch_sources)、对话式画像/日志录入;M4:应用技能(购物/健身)

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt -r requirements-m2.txt
.venv\Scripts\python -m memvault init

# 日常形态:托盘常驻(面板 http://127.0.0.1:8765)
.venv\Scripts\python -m memvault tray
.venv\Scripts\python -m memvault autostart on   # 可选:登录自启(计划任务)

# Chrome: chrome://extensions → 开发者模式 → 加载已解压 → 选择 extension/ 目录
```

采集方式:浏览器插件点图标(网页/商品/视频)/ 全局热键 Ctrl+Alt+B(剪贴板文本或文件路径)/ CLI:

```bash
.venv\Scripts\python -m memvault ingest video BV1xxxx --domain cooking   # 手动采集视频
.venv\Scripts\python -m memvault query "生抽 什么时候放"                   # 命令行检索
```

首次运行会自动下载嵌入模型(bge-small-zh)与 ASR 模型(whisper small),国内网络已默认走 hf-mirror 镜像。

## 测试

```bash
.venv\Scripts\python -m pytest tests/ -v
```

## 技术栈

Python 3.10+ · SQLite(WAL+FTS5) · Chroma · faster-whisper · OpenCV · sentence-transformers(bge) · Playwright(M4) · FastAPI(M2)
