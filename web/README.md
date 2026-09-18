# MemVault 面板前端(Vue 3 + Vite)

面板原来是一套 Jinja2 服务端渲染 + 表单 POST 的页面(9 页 / 17 处 redirect)。
这里记录**渐进增强式迁移**的做法与约定:后端先抽出统一契约的 JSON 层,前端按页迁移,
**旧路由一行不删**,任何一步都能单独回退。

```
浏览器
  │  GET /inbox                     ← Jinja2:只有 base.html 外壳(导航 + 主题变量)
  │  GET /static/dist/inbox.js      ← 本目录构建产物(已入库,托盘直接托管)
  ▼
Vue 视图(InboxView.vue)── fetch ──► /api/inbox ──► FastAPI(memvault/server/api.py)
                                          │  统一契约 {ok, data, error}
                                          ▼
                                   SQLite + Chroma(memvault/memory.py)
```

## 目录

```
web/
  package.json / vite.config.js     # 多入口构建;base=/static/dist/;dev 代理 /api 与 /media
  src/
    entries/<page>.js               # 每迁移一个页面加一个入口(挂载点 → 视图)
    views/<Page>View.vue            # 页面组件(样式沿用面板既有 class)
    api/client.js                   # 契约解包 + 错误归一(ApiError)+ 各页面接口
```

## 构建与开发

```bash
cd web
npm install            # 需要 node ≥ 18(本机 v24;npm 走淘宝镜像)
npm run build          # 产物 → ../memvault/server/static/dist/(已入库,提交即可)

# 热更新调试:先起托盘服务(8765),再
npm run dev            # 5173;/api、/media 已代理到 127.0.0.1:8765
```

> 产物已入库:拉代码就能跑,不必人人装 node。**但改了 `web/src/**` 就要重新 build 并提交产物**,
> 否则页面上看到的还是旧版本。

## 约定(改代码前先看这三条)

1. **接口契约**:所有 `/api/*` 返回 `{ok, data, error}`;失败时 `error = {code, message}`。
   契约解包只在 `src/api/client.js` 做一次,组件里不出现 `fetch`、不拼 URL、不判 `res.ok`。
   错误映射只对 `/api` 路径生效——页面路由保持框架原生形状,便于双轨并存。
2. **样式**:只用 `memvault/server/static/base.css` + `theme.css` 里已有的 class
   (`card` / `tag` / `chips` / `snippet` / `jump` / `muted` …),以及 `--var` 设计变量。
   **不要引入 UI 组件库、不要写死颜色**——否则 `theme-notion` / `theme-flomo` 两个
   主题分支(它们只改 `theme.css` 的变量)会失效。
3. **测试**:页面模板只剩挂载点,所以断言分两层——
   - 页面:`/xxx` 返回 200 且含 `id="xxx-app"` 与 `/static/dist/xxx.js`;
   - 数据/行为:`/api/...` 的 JSON 内容(见 `tests/test_api_json.py`)。
   不要再断言页面 HTML 里的文案。

## 加一个新页面(固定动作)

1. 后端:若缺接口,在 `memvault/server/api.py` 加(带中文 docstring,会进 `/docs`);
2. 前端:`src/api/client.js` 补该页面的接口封装;
3. 视图:`src/views/<Page>View.vue`(先加载 `loading`,失败给提示条,动作用 `busy` 防重复点击);
4. 入口:`src/entries/<page>.js`(`getElementById('xxx-app')` 后 `createApp(...).mount()`),
   并在 `vite.config.js` 的 `rollupOptions.input` 里加一行;
5. 模板:`memvault/server/templates/<page>.html` 改成挂载点 + `<script type="module">`;
   对应页面路由只 `TemplateResponse(..., "xxx.html")` 渲染外壳;
6. `npm run build` → 跑 `pytest tests/ -q` → 用真实浏览器点一遍(本项目用
   Playwright 驱动系统 Chrome 冒烟,见 `docs/testing-log.md`)。

## 回退

每个页面的迁移是独立提交:旧模板在 git 历史里,`git revert <commit>` 即可让该页回到
Jinja2 渲染(后端旧路由一直保留着,不需要改任何后端代码)。

## 踩过的坑(新会话直接抄)

- `base.html` 导航里有搜索框:Playwright 用 `page.fill("input")` 会选到它,
  要用 `input[placeholder*='...']` 之类的精确选择器;
- 挂载点 id 必须与 `entries/*.js` 中的 `getElementById` 一致,拼错不会报错、只会白屏;
- Vite 多入口会做代码分割:`chunks/client-<hash>.js` 是共享的 Vue runtime,
  **提交产物时别只提交 `*.js`**(整个 `static/dist/` 都要);
- 改完后端接口记得重启托盘(`run_tray.py`)——模板是每次请求读盘,但 Python 代码不是。
