// 库首页入口:Jinja2 模板只留一个挂载点。
import { createApp } from 'vue'
import HomeView from '../views/HomeView.vue'

const el = document.getElementById('home-app')
if (el) createApp(HomeView, { initialDomain: el.dataset.domain || '' }).mount(el)
