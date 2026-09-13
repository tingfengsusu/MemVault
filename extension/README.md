# MemVault 浏览器插件(Chrome MV3)

## 安装(开发者模式加载)

1. 先启动本地服务:托盘常驻(推荐)`python -m memvault tray`,或仅服务 `python -m memvault serve`
2. Chrome 打开 `chrome://extensions` → 右上角开启 **开发者模式** → **加载已解压的扩展程序** → 选择本目录(`extension/`)

## 使用

- 插件图标亮 = 服务在线;灰 × = 服务未启动
- **点击插件图标**即采集当前页,角标 ✓ 成功 / ! 失败 / × 服务离线
- 采集规则(自动判断页面类型):
  - **B站视频页**(bilibili.com/video、b23.tv)→ 下载视频完整入库(抽帧+OCR+ASR),含当前播放时间
  - **京东商品页** → 商品条目(名称/价格/主图/店铺)入个人库
  - **淘宝/天猫商品页** → 商品条目(名称/主图)
  - **有选中文字的任意页面** → 采集选中文字
  - **其他页面** → 采集整页正文(前 8000 字)
- 之后在面板(http://127.0.0.1:8765)查看"待整理箱"

## 说明

- 插件只做"取信息 + 发本机",不做下载解析 — 重活全在本地服务
- 端口默认 8765,如修改 config.yaml 的 server.port,需同步改 background.js 的 API_BASE 与 manifest.json 的 host_permissions
