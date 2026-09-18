// 分类管理入口:Jinja2 模板只留一个挂载点。
import { createApp } from 'vue'
import CategoriesView from '../views/CategoriesView.vue'

const el = document.getElementById('categories-app')
if (el) createApp(CategoriesView).mount(el)
