import { createApp } from 'vue'
import SettingsView from '../views/SettingsView.vue'
const el = document.getElementById('settings-app')
if (el) createApp(SettingsView).mount(el)
