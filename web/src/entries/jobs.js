import { createApp } from 'vue'
import JobsView from '../views/JobsView.vue'
const el = document.getElementById('jobs-app')
if (el) createApp(JobsView).mount(el)
