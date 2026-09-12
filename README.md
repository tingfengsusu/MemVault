# MemVault

本地优先的**个人多模态记忆库**:把你在视频、网页、文档里遇到的内容一键采集进个人库,经抽帧/OCR/ASR/嵌入后存入 SQLite + 向量库,支撑混合检索、溯源跳转与个性化应用(购物决策、健身教练……)。

> 设计文档见 [DESIGN.md](DESIGN.md)。前作 [Video2Shop](https://github.com/tingfengsusu/Video2Shop)(视频→食材清单→京东加购)的管线已吸收为采集源之一。

## 当前状态:M1(平台地基)

- ✅ SQLite 九张表(items/chunks/categories/profile/logs/jobs/prompts/prompt_feedback/watch_sources)+ FTS5 中文全文
- ✅ Chroma 向量库(文本 bge-small-zh;图像 CLIP 可选)
- ✅ 视频采集管线:B站下载(移植)→ HSV 场景抽帧(带时间戳)→ OCR 存档(可选)→ faster-whisper ASR → 入库
- ✅ 记忆 API:混合检索(向量 + 关键词 RRF 融合)、画像、日志、时间线
- ✅ CLI 验收:`ingest` / `query` / `stats`
- ⏳ M2:托盘常驻 + 浏览器插件;M3:分类与提示词进化;M4:应用技能

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -r requirements-dev.txt
.venv\Scripts\python -m memvault init

# 采集一条视频(B站 URL/BV 号或本地文件)
.venv\Scripts\python -m memvault ingest video BV1xxxx --domain cooking

# 检索(命中带时间戳,可跳回视频)
.venv\Scripts\python -m memvault query "生抽 什么时候放"

# 可选:OCR 存档与图像嵌入
.venv\Scripts\pip install -r requirements-vision.txt
```

首次运行会自动下载嵌入模型(bge-small-zh)与 ASR 模型(whisper small),国内网络已默认走 hf-mirror 镜像。

## 测试

```bash
.venv\Scripts\python -m pytest tests/ -v
```

## 技术栈

Python 3.10+ · SQLite(WAL+FTS5) · Chroma · faster-whisper · OpenCV · sentence-transformers(bge) · Playwright(M4) · FastAPI(M2)
