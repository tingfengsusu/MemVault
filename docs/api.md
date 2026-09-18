# MemVault HTTP 接口清单

> 由 `python temp/make_evidence.py` 从运行中服务的 OpenAPI 自动导出(重跑该脚本可刷新截图与本文档)。

统一响应契约(所有 `/api/*`):

```json
成功 {"ok": true,  "data": {...}, "error": null}
失败 {"ok": false, "data": null,  "error": {"code": "...", "message": "..."}}
```

分页统一带 `total` / `page` / `page_size`。**错误映射只对 `/api` 路径生效**,
页面路由保持框架原生形状(便于渐进迁移时的双轨并存)。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 库首页(外壳;数据由 /api/items + /api/stats 提供)。 |
| POST | `/api/capture` | 采集入口(见 DESIGN §8.1):selection/page/product/video。 |
| GET | `/api/categories` | 分类树 + 按领域分组 + UP主规则 + 领域候选(前端下拉用)。 |
| POST | `/api/categories` | 新增分类(领域可手填,也可用 /api/categories 里的 domains 建议)。 |
| POST | `/api/categories/{category_id}/confirm` | 采纳 LLM 提议的待确认分类。 |
| POST | `/api/categories/{category_id}/delete` | 删除分类:条目退回待整理箱,专属提示词与 UP 规则一并清理。 |
| POST | `/api/chat` | 与技能对话(LLM 抽取画像/日志 → 检索个人上下文 → 生成回复)。 |
| GET | `/api/health` | 探活:插件/脚本用它判断服务是否在线。 |
| GET | `/api/inbox` | 待整理箱:列表 + 可选的分类树(前端下拉用,按领域分组)。 |
| POST | `/api/inbox/batch` | 批量处理:{action, ids?, domain?, category_id?} |
| GET | `/api/items` | 检索条目:q 走混合检索(向量+关键词),否则按 domain/status 过滤分页。 |
| GET | `/api/items/{item_id}` | 条目详情:语义块(带时间戳/B站跳转)、AI 属性、相关条目、UP 信息。 |
| POST | `/api/items/{item_id}/bind-up` | 把这个 UP 的视频都归到该分类(可含本条:直接归档)。 |
| POST | `/api/items/{item_id}/cart` | 把条目加入京东购物车(用户显式点击,worker 异步执行)。 |
| POST | `/api/items/{item_id}/classify` | 手动归入指定分类(待整理箱的"归入所选分类")。 |
| POST | `/api/items/{item_id}/reanalyze` | 重新分析(交给队列:路由 + 提取)。 |
| POST | `/api/items/{item_id}/similar-image` | 用库里已有的某帧找相似画面(条目详情页「🔍 找相似画面」)。 |
| POST | `/api/items/{item_id}/status` | 改条目状态:inbox / filed / archived。 |
| GET | `/api/jobs` | 任务队列(含 payload 摘要与耗时)。 |
| POST | `/api/pause` | 暂停/恢复采集开关。 |
| POST | `/api/search/image` | 以图搜图(JSON 版):上传一张图,返回相似画面列表(含查询图回显地址)。 |
| GET | `/api/search/images` | 文字搜画面(Chinese-CLIP);未装权重时返回空列表而非报错。 |
| GET | `/api/settings` | 当前配置(**密钥只回显是否已配置,绝不返回明文**)。 |
| POST | `/api/settings` | 保存 LLM 设置(写 config.yaml + .env 并即时生效)。 |
| POST | `/api/settings/test` | 测试 LLM 连通性:返回 {ok, message}。 |
| GET | `/api/sources` | 订阅源列表(B站 UP主等)。 |
| POST | `/api/sources` | 新增订阅源(B站 UP主):UID 或 space 链接都能解析。 |
| POST | `/api/sources/{source_id}/check` | 立即检查该订阅源(入队 watch_check)。 |
| POST | `/api/sources/{source_id}/toggle` | 启用/停用订阅源。 |
| GET | `/api/stats` | 库概览:条目/语义块/待整理/日志等计数 + 领域分布。 |
| POST | `/api/up/{up_mid}/unbind` | 解除某个 UP 的分类规则。 |
| GET | `/categories` | 分类管理页(外壳;数据由 /api/categories 提供)。 |
| POST | `/categories/add` | 旧表单:新增分类(双轨保留)。 |
| POST | `/categories/{category_id}/confirm` | 旧表单:采纳提议分类(双轨保留)。 |
| POST | `/categories/{category_id}/delete` | 删除分类:条目退回待整理箱,专属提示词/UP规则一并清理。 |
| GET | `/chat` | 聊天页(外壳;对话走 POST /api/chat)。 |
| GET | `/inbox` | 待整理箱(外壳;数据由 /api/inbox 提供)。 |
| POST | `/inbox/batch` | 批量处理待整理箱:filed/archived 直接改状态,auto 交给 LLM 逐条分类。 |
| GET | `/items/{item_id}` | 条目详情页(外壳;数据由 /api/items/{id} 提供)。 |
| POST | `/items/{item_id}/bind-up` | 把这个 UP 的视频都归到该分类(可含本条:`filed` 直接归档)。 |
| POST | `/items/{item_id}/cart` | 把库中条目加入京东购物车(用户显式点击,worker 异步执行)。 |
| POST | `/items/{item_id}/classify` | 手动把条目归入指定分类。 |
| POST | `/items/{item_id}/reanalyze` | 旧表单:重新分析(双轨保留)。 |
| POST | `/items/{item_id}/status` | 旧表单:改状态后回待整理箱(revert 用,双轨保留)。 |
| GET | `/jobs` | 任务页(外壳;数据由 /api/jobs 提供)。 |
| GET | `/search` | 检索页(外壳;数据由 /api/items + /api/search/images 提供)。 |
| GET | `/settings` | 设置页(外壳;数据由 /api/settings 提供)。 |
| POST | `/settings/save` | 写回 config.yaml(后端/base_url/model/阈值)与 .env(密钥),即时生效。 |
| POST | `/settings/test` | 旧表单:测试 LLM 连接(双轨保留)。 |
| GET | `/sources` | 订阅页(外壳;数据由 /api/sources 提供)。 |
| POST | `/sources/add` | 旧表单:新增订阅(双轨保留)。 |
| POST | `/sources/{source_id}/check` | 旧表单:立即检查订阅(双轨保留)。 |
| POST | `/sources/{source_id}/toggle` | 旧表单:启停订阅(双轨保留)。 |
| POST | `/up/{up_mid}/unbind` | 旧表单:解除 UP 规则(双轨保留)。 |

## 统一契约的 JSON 接口(`/api/*`)

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/capture` | 采集入口(见 DESIGN §8.1):selection/page/product/video。 |
| GET | `/api/categories` | 分类树 + 按领域分组 + UP主规则 + 领域候选(前端下拉用)。 |
| POST | `/api/categories` | 新增分类(领域可手填,也可用 /api/categories 里的 domains 建议)。 |
| POST | `/api/categories/{category_id}/confirm` | 采纳 LLM 提议的待确认分类。 |
| POST | `/api/categories/{category_id}/delete` | 删除分类:条目退回待整理箱,专属提示词与 UP 规则一并清理。 |
| POST | `/api/chat` | 与技能对话(LLM 抽取画像/日志 → 检索个人上下文 → 生成回复)。 |
| GET | `/api/health` | 探活:插件/脚本用它判断服务是否在线。 |
| GET | `/api/inbox` | 待整理箱:列表 + 可选的分类树(前端下拉用,按领域分组)。 |
| POST | `/api/inbox/batch` | 批量处理:{action, ids?, domain?, category_id?} |
| GET | `/api/items` | 检索条目:q 走混合检索(向量+关键词),否则按 domain/status 过滤分页。 |
| GET | `/api/items/{item_id}` | 条目详情:语义块(带时间戳/B站跳转)、AI 属性、相关条目、UP 信息。 |
| POST | `/api/items/{item_id}/bind-up` | 把这个 UP 的视频都归到该分类(可含本条:直接归档)。 |
| POST | `/api/items/{item_id}/cart` | 把条目加入京东购物车(用户显式点击,worker 异步执行)。 |
| POST | `/api/items/{item_id}/classify` | 手动归入指定分类(待整理箱的"归入所选分类")。 |
| POST | `/api/items/{item_id}/reanalyze` | 重新分析(交给队列:路由 + 提取)。 |
| POST | `/api/items/{item_id}/similar-image` | 用库里已有的某帧找相似画面(条目详情页「🔍 找相似画面」)。 |
| POST | `/api/items/{item_id}/status` | 改条目状态:inbox / filed / archived。 |
| GET | `/api/jobs` | 任务队列(含 payload 摘要与耗时)。 |
| POST | `/api/pause` | 暂停/恢复采集开关。 |
| POST | `/api/search/image` | 以图搜图(JSON 版):上传一张图,返回相似画面列表(含查询图回显地址)。 |
| GET | `/api/search/images` | 文字搜画面(Chinese-CLIP);未装权重时返回空列表而非报错。 |
| GET | `/api/settings` | 当前配置(**密钥只回显是否已配置,绝不返回明文**)。 |
| POST | `/api/settings` | 保存 LLM 设置(写 config.yaml + .env 并即时生效)。 |
| POST | `/api/settings/test` | 测试 LLM 连通性:返回 {ok, message}。 |
| GET | `/api/sources` | 订阅源列表(B站 UP主等)。 |
| POST | `/api/sources` | 新增订阅源(B站 UP主):UID 或 space 链接都能解析。 |
| POST | `/api/sources/{source_id}/check` | 立即检查该订阅源(入队 watch_check)。 |
| POST | `/api/sources/{source_id}/toggle` | 启用/停用订阅源。 |
| GET | `/api/stats` | 库概览:条目/语义块/待整理/日志等计数 + 领域分布。 |
| POST | `/api/up/{up_mid}/unbind` | 解除某个 UP 的分类规则。 |

## 旧页面 / 表单路由(渐进迁移期双轨保留)

页面路由只渲染外壳(数据由上面的 JSON 接口提供);表单路由供旧链接与回退使用。

| 方法 | 路径 |
|---|---|
| GET | `/` |
| GET | `/categories` |
| POST | `/categories/add` |
| POST | `/categories/{category_id}/confirm` |
| POST | `/categories/{category_id}/delete` |
| GET | `/chat` |
| GET | `/inbox` |
| POST | `/inbox/batch` |
| GET | `/items/{item_id}` |
| POST | `/items/{item_id}/bind-up` |
| POST | `/items/{item_id}/cart` |
| POST | `/items/{item_id}/classify` |
| POST | `/items/{item_id}/reanalyze` |
| POST | `/items/{item_id}/status` |
| GET | `/jobs` |
| GET | `/search` |
| GET | `/settings` |
| POST | `/settings/save` |
| POST | `/settings/test` |
| GET | `/sources` |
| POST | `/sources/add` |
| POST | `/sources/{source_id}/check` |
| POST | `/sources/{source_id}/toggle` |
| POST | `/up/{up_mid}/unbind` |

共 54 个端点(JSON 30 + 旧路由 24);交互式文档:服务运行时访问 `/docs`。
