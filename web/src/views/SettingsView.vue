<script setup>
/**
 * 设置(Vue 版):LLM 通道 / 模型 / 阈值 + 测试连接。
 * 密钥只写不回读:接口回显 api_key_set 布尔值,输入框留空表示"不改动现有 key"。
 * 数据:GET /api/settings;动作:POST /api/settings、POST /api/settings/test
 */
import { computed, onMounted, ref } from 'vue'
import { ApiError, api, settingsApi } from '../api/client.js'

const s = ref({ llm: {}, asr: {}, embedding: {}, vision: {}, frames: {}, links: {} })
const loading = ref(true)
const busy = ref(false)
const toast = ref(null)
const testResult = ref(null)
const keyInput = ref('')

const form = ref({ backend: 'api', base_url: '', model: '', classify_confidence: 0.8 })
const keyHint = computed(() => keyInput.value
  ? '将替换现有 key'
  : (s.value.llm?.api_key_set ? '已配置(留空则不改动)' : '未配置'))

function flash(kind, text) {
  toast.value = { kind, text }
  setTimeout(() => { if (toast.value?.text === text) toast.value = null }, 6000)
}

async function load() {
  loading.value = true
  try {
    s.value = await api.get('/api/settings')
    form.value = {
      backend: s.value.llm.backend || 'api',
      base_url: s.value.llm.base_url || '',
      model: s.value.llm.model || '',
      classify_confidence: s.value.llm.classify_confidence ?? 0.8,
    }
  } catch (e) {
    flash('err', e instanceof ApiError ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

async function save() {
  busy.value = true
  try {
    const d = await settingsApi.save({ ...form.value, api_key: keyInput.value })
    keyInput.value = ''
    flash('ok', `已保存并即时生效(backend=${d.backend}, model=${d.model}${d.api_key_updated ? ', 密钥已更新' : ''})`)
    await load()
  } catch (e) {
    flash('err', e.message)
  } finally {
    busy.value = false
  }
}

async function testConn() {
  busy.value = true
  testResult.value = null
  try {
    const d = await settingsApi.test()
    testResult.value = { kind: 'ok', text: d.message }
  } catch (e) {
    testResult.value = { kind: 'err', text: e.message }
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <h3 style="margin:4px 0 12px">设置</h3>

    <div v-if="toast" class="card">
      <span :class="toast.kind === 'err' ? 'tag warn' : 'tag'">
        {{ toast.kind === 'err' ? '出错' : '完成' }}</span>
      <span class="muted" style="margin-left:8px">{{ toast.text }}</span>
    </div>

    <div class="card">
      <h3>LLM 通道</h3>
      <form style="display:grid;gap:10px;max-width:620px" @submit.prevent="save">
        <label style="display:flex;gap:8px;align-items:center">
          <input type="radio" value="api" v-model="form.backend"> API(付费稳定)
        </label>
        <label style="display:flex;gap:8px;align-items:center">
          <input type="radio" value="web" v-model="form.backend"> 网页通道(免 token,Playwright 驱动)
        </label>
        <label>Base URL
          <input v-model="form.base_url" required
                 style="width:100%;padding:6px 10px;border:1px solid var(--input-border);border-radius:6px">
        </label>
        <label>模型
          <input v-model="form.model" required
                 style="width:100%;padding:6px 10px;border:1px solid var(--input-border);border-radius:6px">
        </label>
        <label>API 密钥({{ keyHint }})
          <input v-model="keyInput" type="password" placeholder="留空 = 不改动"
                 style="width:100%;padding:6px 10px;border:1px solid var(--input-border);border-radius:6px">
        </label>
        <label>分类置信度阈值
          <input v-model.number="form.classify_confidence" type="number" step="0.05" min="0" max="1"
                 style="width:120px;padding:6px 10px;border:1px solid var(--input-border);border-radius:6px">
        </label>
        <div style="display:flex;gap:8px;align-items:center">
          <button :disabled="busy">保存并生效</button>
          <button type="button" class="ghost" :disabled="busy" @click="testConn">🔌 测试连接</button>
          <span v-if="testResult" :class="testResult.kind === 'err' ? 'tag warn' : 'tag'">
            {{ testResult.text }}</span>
        </div>
      </form>
    </div>

    <div class="card">
      <h3>当前生效配置</h3>
      <table>
        <tr><th style="width:150px">ASR</th>
            <td>{{ s.asr.model }} · device={{ s.asr.device }} · {{ s.asr.compute_type }}</td></tr>
        <tr><th>抽帧</th>
            <td>最多 {{ s.frames.max_frames }} 帧 · 间隔 {{ s.frames.frame_interval }}s ·
                场景阈值 {{ s.frames.scene_threshold }}</td></tr>
        <tr><th>OCR</th>
            <td>{{ s.vision.ocr?.enabled }} · 人声占比低于 {{ s.vision.ocr?.speech_ratio }} 才跑</td></tr>
        <tr><th>图像嵌入</th>
            <td>{{ s.vision.image_embed?.enabled }}(Chinese-CLIP,缺权重自动跳过)</td></tr>
        <tr><th>双链</th>
            <td>阈值 {{ s.links.similarity_threshold }} · 每条最多 {{ s.links.max_per_item }} 条</td></tr>
        <tr><th>文本嵌入</th>
            <td>{{ s.embedding.text_model }}{{ s.embedding.fake ? '(fake 模式)' : '' }}</td></tr>
      </table>
      <p class="muted" style="margin:8px 0 0">
        这些值来自 <b>config.yaml</b>(改文件后重启生效);上面表单只覆盖 LLM 相关项。</p>
    </div>
  </div>
</template>
