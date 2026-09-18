// 待整理箱入口:Jinja2 模板只留一个挂载点,其余交给 Vue。
import { createApp } from 'vue'
import InboxView from '../views/InboxView.vue'

const el = document.getElementById('inbox-app')
if (el) {
  createApp(InboxView, { initialDomain: el.dataset.domain || '' }).mount(el)
}
