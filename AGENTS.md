# AGENTS.md — MemVault 项目工作约定

> 本文件供在该目录工作的 AI 会话阅读并遵守（人也可以看）。
> 最后更新：2026-09-17。

## 项目速览

MemVault：本地优先的个人多模态记忆库（视频/网页/商品 → 语义块入库 → 混合检索 → LLM 分类与技能执行）。

- 设计文档：`DESIGN.md`（§13 记录各里程碑与变更）
- 测试与问题记录：`docs/testing-log.md`（复查入口，问题与解决方式都在这里）
- 数据目录：`data/`（SQLite + Chroma，不入 git）；配置：`config/config.yaml`
- 测试：`pytest tests/ -q`（当前 53 例）；改代码后必须全绿

## 服务管理（最重要：必须分离式启动）

`python -m memvault tray` 是**常驻进程，永不退出**。前面直接跑它（前台或任务里），
任务系统会永远显示“正在获取任务输出”并挂起——这是设计使然，不是故障。

**已实测确认的根因（2026-09-17，勿再踩）**：工具调用结束的条件是**整条命令的进程树/管道全部结束**。
只要还有存活成员，调用就永远不结束：

1. **前台跑 tray** → 命令本体永不退出 → 卡死；
2. **启动命令里接 `| tail` / `| head` / 任何原生管道读取器** → 被启动的托盘继承了
   管道的写端句柄，读取器永远等不到 EOF → 卡死（**即使 Start-Process 重定向了
   stdout/stderr 也没用**，这正是父会话踩的坑）。
3. 只有部分命令能幸免：纯 PowerShell 内存内管道（如 `| Select-Object -ExpandProperty Id`）
   不产生额外进程，因此不阻塞。

### 正确做法（三条铁律）

1. **启动/重启命令独立成一次工具调用**，里面只放启动动作，不与其他工作串联；
   健康检查放到下一次调用（`curl -s http://127.0.0.1:8765/api/health`）；
2. **绝不拼原生管道**——不要 `| tail -1`、`2>&1 | tail`、`| head`；需要看输出就
   重定向到文件再另起一次调用读取；
3. 启动调用**优先以“解除沙箱”权限执行**（本环境实测：同样命令解除沙箱后秒回）；
   或使用 schtasks 托管：`schtasks /Run /TN MemVault`（装过 autostart 才有）。

```powershell
# 1) 停旧实例（按命令行匹配，连壳带真身一起停——只杀一半会剩僵尸）
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object {$_.CommandLine -like '*run_tray*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"

# 2) 分离式启动（独立调用；不接任何管道；解除沙箱执行最佳）
powershell -NoProfile -Command "$env:HF_HUB_OFFLINE='1'; Start-Process -FilePath 'D:\Code\MemVault\.venv\Scripts\python.exe' -ArgumentList '-u','D:\Code\MemVault\run_tray.py' -WorkingDirectory 'D:\Code\MemVault' -WindowStyle Hidden -RedirectStandardOutput 'D:\Code\MemVault\data\tray.log' -RedirectStandardError 'D:\Code\MemVault\data\tray.err.log' -PassThru | Select-Object -ExpandProperty Id"

# 3) 下一次调用再验证就绪
curl -s http://127.0.0.1:8765/api/health
```

### 卡住后的解卡手册（已实践验证）

1. 找挂起成员：扫创建时间较早、仍存活的 `bash/powershell/**tail**/python` 进程；
2. **杀掉管道读取器**（`tail.exe` 之类）或前台进程——任务立刻进入终态
   （日志出现 `background_task.tracking.terminal`），服务通常不受影响；
3. 若卡住的树包含托盘本体，则杀托盘对并**立即按上面步骤重启**（服务中断约半分钟）；
4. 核对是否全部闭环：对比日志中 `background_task.tracking.started` 与 `terminal` 的任务 id。

### 本机特有的坑（均已实测）

1. **Python 是“壳 + 真身”一对进程**：venv 的 `python.exe` 启动后会派生
   `D:\CodeLanguage\python.exe` 子进程干真正的活（监听 8765 的是它）。
   两个都要留着；只杀其中任意一个都可能把服务弄停（杀父会带走子）。
   重启时按命令行 `*run_tray*` 全部停掉再重新启动，才是干净做法。
2. **必须带 `HF_HUB_OFFLINE=1`**：模型已在本机缓存，但 huggingface_hub 启动时
   仍会联网校验；hf-mirror 不可达时会重试数分钟，表现为“启动很慢”。
   离线模式让启动稳定在几秒。
3. **不要等待常驻任务的“完成”**：它永远不会完成。判断成功的唯一方式是
   `/api/health` 返回 `{"ok": true}`，或浏览器能打开 http://127.0.0.1:8765。

## 常用命令

```
.venv\Scripts\python -m memvault ingest video <BV号|本地文件> --domain <领域>
.venv\Scripts\python -m memvault query "关键词"
.venv\Scripts\python -m memvault reindex          # 换嵌入模型后重建全部文本向量
.venv\Scripts\python scripts/gen_testdata.py      # 第一轮模拟数据
.venv\Scripts\python scripts/gen_longterm.py      # 长期使用压力模拟
.venv\Scripts\python scripts/check_integrity.py   # 数据体检（应 ALL_PASS）
```

## 开发约定

- 分支：`main`（默认，「清雅」浅色主题）；`theme-notion`、`theme-flomo` 为纯主题分支——
  **只改 `memvault/server/static/theme.css` 的设计变量**；结构/功能改动只在 main 上做，
  完成后 `git merge main` 同步到两个主题分支。
- 提交前：`pytest tests/ -q` 全绿；涉及数据层的改动在 `docs/testing-log.md`
  追加记录（问题/根因/解决/验证）。
- GitHub 推送偶发连接重置：重试循环（最多 6 次、间隔几秒）再放弃，本地提交不受影响。
- 秘密文件：`config/cookies.txt`、`.env` 已被 gitignore，**永远不要提交或输出其内容**。
- B站订阅依赖 `config.bili.cookies_path`（登录 cookies）；被风控（-352）时提示用户重新导出。
