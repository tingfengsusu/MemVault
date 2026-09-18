import { createApp } from 'vue'
import ChatView from '../views/ChatView.vue'
const el = document.getElementById('chat-app')
if (el) createApp(ChatView).mount(el)
