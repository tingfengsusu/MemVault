import { createApp } from 'vue'
import SourcesView from '../views/SourcesView.vue'
const el = document.getElementById('sources-app')
if (el) createApp(SourcesView).mount(el)
