<script setup>
/**
 * 聊天(Vue 版):技能选择 + 对话 + 抽取提示。
 * 数据与动作:POST /api/chat(消息不落盘,抽取出的画像/日志落盘)。
 * 相比旧版加了两处体感改进:①等待时显示已用秒数 ②出错时给出可操作的提示。
 */
import { nextTick, onMounted, ref } from 'vue'
import { ApiError, chatApi } from '../api/client.js'

const skills = [
  { id: 'general', name: '通用助手' },
  { id: 'fitness', name: '健身教练' },
  { id: 'shopping', name: '购物决策' },
]
const skill = ref('general')
const input = ref('')
const messages = ref([])          // {who, text, meta?, kind?}
const sending = ref(false)
const elapsed = ref(0)
const logEl = ref(null)
let timer = null

function push(who, text, meta = '', kind = '') {
  messages.value.push({ who, text, meta, kind })
  nextTick(() => logEl.value?.scrollTo({ top: logEl.value.scrollHeight }))
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  input.value = ''
  push('我', text)
  sending.value = true
  elapsed.value = 0
  timer = setInterval(() => { elapsed.value += 1 }, 1000)
  try {
    const d = await chatApi.send(text, skill.value)
    const bits = []
    if (d.profile_saved?.length) bits.push('已存入画像:' + d.profile_saved.join('、'))
    if (d.log_saved) bits.push('已记入日志')
    if (d.context_used) bits.push('检索命中 ' + d.context_used + ' 条')
    push('MemVault', d.reply, bits.join(' · '))
  } catch (e) {
    const msg = e instanceof ApiError ? e.message : String(e)
    push('系统', msg, e.code === 'llm_disabled'
      ? '到「设置」页填 API 密钥即可启用' : '', 'err')
  } finally {
    clearInterval(timer)
    timer = null
    sending.value = false
  }
}

function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
}

onMounted(() => {
  push('MemVault', '你好,我是 MemVault 助手。可以说「我身高 178,目标增肌」这类个人事实(会存进画像),'
    + '或「今天练了胸 5 组 + 跑步 40 分钟」(会记入日志),也可以直接问库里有什么。')
})
</script>

<template>
  <div>
    <div class="card">
      <div style="display:flex;gap:10px;align-items:center;margin-bottom:8px;flex-wrap:wrap">
        <b>技能:</b>
        <select v-model="skill"
                style="padding:5px 8px;border:1px solid var(--input-border);border-radius:6px">
          <option v-for="s in skills" :key="s.id" :value="s.id">{{ s.name }}</option>
        </select>
        <span class="muted">对话中提到的个人事实会存入画像,报告的行为会记入日志(回复下方会提示)。</span>
      </div>

      <div ref="logEl" style="max-height:460px;overflow-y:auto">
        <div v-for="(m, i) in messages" :key="i" class="card"
             :style="m.kind === 'err' ? { borderColor: '#c0392b' } : {}">
          <span :class="m.kind === 'err' ? 'tag warn' : 'tag'">{{ m.who }}</span>
          <div class="snippet" style="margin-top:6px">{{ m.text }}</div>
          <div v-if="m.meta" class="muted" style="margin-top:4px">{{ m.meta }}</div>
        </div>
        <div v-if="sending" class="card">
          <span class="tag">MemVault</span>
          <div class="snippet" style="margin-top:6px">
            思考中…({{ elapsed }}s)
            <span class="muted">两次串行 LLM 调用,通常 3~10 秒</span>
          </div>
        </div>
      </div>

      <div style="display:flex;gap:8px;margin-top:10px">
        <input v-model="input" :disabled="sending"
               placeholder="例如:我身高 178,目标增肌。今天练了胸 5 组 + 跑步 40 分钟,明天练什么?"
               style="flex:1;padding:8px 10px;border:1px solid var(--input-border);border-radius:6px"
               @keydown="onKeydown">
        <button :disabled="sending || !input.trim()" @click="send">发送</button>
      </div>
    </div>
  </div>
</template>
