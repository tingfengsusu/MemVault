// 条目详情入口:Jinja2 模板只留一个挂载点。
import { createApp } from 'vue'
import ItemView from '../views/ItemView.vue'

const el = document.getElementById('item-app')
if (el) createApp(ItemView, { itemId: el.dataset.itemId }).mount(el)
