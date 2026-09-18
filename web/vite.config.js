import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

// 构建产物直接落到后端静态目录 → 托盘原样托管,不需要额外的部署步骤。
// 页面由 Jinja2 模板包一层(base.html 提供导航与主题),Vue 只接管 #xxx-app 那一块。
export default defineConfig({
  plugins: [vue()],
  base: '/static/dist/',
  build: {
    outDir: resolve(__dirname, '../memvault/server/static/dist'),
    emptyOutDir: true,
    target: 'es2020',
    rollupOptions: {
      input: {
        // 每迁移一个页面就加一个入口(渐进增强:未迁移的页面仍是 Jinja2 渲染)
        inbox: resolve(__dirname, 'src/entries/inbox.js'),
        search: resolve(__dirname, 'src/entries/search.js'),
        item: resolve(__dirname, 'src/entries/item.js'),
        home: resolve(__dirname, 'src/entries/home.js'),
        categories: resolve(__dirname, 'src/entries/categories.js'),
        jobs: resolve(__dirname, 'src/entries/jobs.js'),
        sources: resolve(__dirname, 'src/entries/sources.js'),
        settings: resolve(__dirname, 'src/entries/settings.js'),
      },
      output: {
        entryFileNames: '[name].js',
        chunkFileNames: 'chunks/[name]-[hash].js',
        assetFileNames: '[name][extname]',
      },
    },
  },
  server: {
    // npm run dev:5173 时把接口与媒体转发到托盘服务,便于热更新调试
    proxy: {
      '/api': 'http://127.0.0.1:8765',
      '/media': 'http://127.0.0.1:8765',
    },
  },
})
