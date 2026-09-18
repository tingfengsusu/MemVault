// 检索页入口:Jinja2 模板只留一个挂载点。
import { createApp } from 'vue'
import SearchView from '../views/SearchView.vue'

const el = document.getElementById('search-app')
if (el) {
  createApp(SearchView, { initialQuery: el.dataset.query || '',
                           similar: el.dataset.similar || '' }).mount(el)
}
