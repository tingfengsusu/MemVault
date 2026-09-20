<script setup>
/**
 * 设置(Vue 版):LLM 通道 / 模型 / 阈值 + 测试连接。
 * 密钥只写不回读:接口回显 api_key_set 布尔值,输入框留空表示"不改动现有 key"。
 * 数据:GET /api/settings;动作:POST /api/settings、POST /api/settings/test
 */
import { computed, onMounted, ref } from 'vue'
import ToastHost from '../components/ToastHost.vue'
import { showToast } from '../lib/toast.js'
import { ApiError, api, bindingApi, settingsApi } from '../api/client.js'

const s = ref({ llm: {}, asr: {}, embedding: {}, vision: {}, frames: {},
                links: {}, clips: {}, usage: {} })
const loading = ref(true)
const busy = ref(false)
const testResult = ref(null)
const keyInput = ref('')

const form = ref({ backend: 'api', base_url: '', model: '', classify_confidence: 0.8,
                   ads_policy: 'ignore' })
const keyHint = computed(() => keyInput.value
  ? '将替换现有 key'
  : (s.value.llm?.api_key_set ? '已配置(留空则不改动)' : '未配置'))

const flash = (kind, text) => showToast(kind, text)

// 提示词规则绑定(购物稿 ①)+ UP 画像缓存(抽帧稿第 4 步)的管理面
const bindings = ref([])
const profiles = ref([])
const kinds = ref([])
const stages = ref(['extract'])
const caches = ref([])
const newRule = ref({ kind: 'up', target: '', prompt_name: '', stage: 'extract', note: '' })

// 值走英文(接口契约),界面显示中文
const KIND_LABELS = { up: 'UP主', up_set: 'UP集', domain: '领域',
                      source_type: '来源类型', keyword: '标题关键词' }
const STAGE_LABELS = { extract: '提取(内容描述)', router: '分类(路由)',
                       clip: '关键片段', frames: '抽帧画像' }
const PROFILE_LABELS = {
  extract: '通用提取(按分类选题)',
  shopping_review: '购物复盘(商品/卖点/证据)',
}
const profileLabel = (p) => PROFILE_LABELS[p] ? `${PROFILE_LABELS[p]} · ${p}` : p
const kindLabel = (k) => KIND_LABELS[k] || k
const stageLabel = (st) => STAGE_LABELS[st] || st

async function loadBindings() {
  try {
    const d = await bindingApi.list()
    bindings.value = d.items
    profiles.value = d.profiles
    kinds.value = d.kinds
    stages.value = d.stages
    caches.value = d.caches
    if (!newRule.value.prompt_name && d.profiles.length) {
      newRule.value.prompt_name = d.profiles[d.profiles.length - 1]
    }
  } catch (e) { flash('err', e.message) }
}

async function addRule() {
  busy.value = true
  try {
    await bindingApi.add({ ...newRule.value })
    flash('ok', '规则已绑定')
    newRule.value.target = ''
    newRule.value.note = ''
    await loadBindings()
  } catch (e) { flash('err', e.message) } finally { busy.value = false }
}

async function removeRule(r) {
  busy.value = true
  try {
    await bindingApi.remove(r.kind, r.target, r.stage)
    flash('ok', '规则已解除')
    await loadBindings()
  } catch (e) { flash('err', e.message) } finally { busy.value = false }
}

async function clearCache(c) {
  busy.value = true
  try {
    await bindingApi.clearCache(c.up_mid)
    flash('ok', '画像缓存已清除(下次采集重新生成)')
    await loadBindings()
  } catch (e) { flash('err', e.message) } finally { busy.value = false }
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
      ads_policy: s.value.llm.ads_policy || 'ignore',
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

onMounted(() => { load(); loadBindings() })
</script>

<template>
  <div>
    <h3 style="margin:4px 0 12px">设置</h3>
    <ToastHost />

    <div class="card">
      <h3>提示词规则 <span class="muted" style="font-weight:400">
        命中规则的条目用指定 profile(如购物复盘),不再吃分类提示词</span></h3>
      <div v-if="bindings.length" style="margin-bottom:8px">
        <div v-for="r in bindings" :key="r.kind + r.target + r.stage"
             style="display:flex;gap:10px;align-items:center;padding:6px 0;border-bottom:1px dashed var(--card-border)">
          <span class="tag gray">{{ kindLabel(r.kind) }}</span>
          <span style="min-width:150px">
            {{ r.label || '—' }}<span class="muted"> {{ r.target }}</span></span>
          <span class="tag" :title="r.prompt_name">{{ profileLabel(r.prompt_name) }}</span>
          <span class="muted">{{ stageLabel(r.stage) }}</span>
          <span class="muted" style="font-size:12px">{{ r.note || '' }}</span>
          <button class="ghost" :disabled="busy" @click="removeRule(r)">解除</button>
        </div>
      </div>
      <p v-else class="muted">还没有规则。示例:某 UP主 → shopping_review,让他的视频走购物复盘提取。</p>
      <form style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px"
            @submit.prevent="addRule">
        <select v-model="newRule.kind">
          <option v-for="k in kinds" :key="k" :value="k">{{ kindLabel(k) }}</option>
        </select>
        <input v-model="newRule.target"
               :placeholder="newRule.kind === 'up' ? 'UP 的 mid(如 17495873)'
                 : newRule.kind === 'up_set' ? '多个 mid,逗号分隔'
                 : newRule.kind === 'domain' ? '领域,如 shopping'
                 : newRule.kind === 'source_type' ? '来源类型,如 video'
                 : '标题里出现的关键词'"
               required style="min-width:230px">
        <select v-model="newRule.prompt_name">
          <option v-for="p in profiles" :key="p" :value="p">{{ profileLabel(p) }}</option>
        </select>
        <select v-model="newRule.stage">
          <option v-for="st in stages" :key="st" :value="st">{{ stageLabel(st) }}</option>
        </select>
        <input v-model="newRule.note" placeholder="备注(可空)" style="min-width:120px">
        <button :disabled="busy">绑定</button>
      </form>
    </div>

    <div v-if="caches.length" class="card">
      <h3>UP 画像缓存 <span class="muted" style="font-weight:400">
        探针为该 UP 记住的常规形态(字幕带/事件频率/人声占比)</span></h3>
      <div v-for="c in caches" :key="c.up_mid"
           style="display:flex;gap:10px;align-items:center;padding:6px 0;border-bottom:1px dashed var(--card-border)">
        <span class="tag gray">mid {{ c.up_mid }}</span>
        <span class="muted">{{ c.label || '' }} 字幕带
          {{ (c.subtitle_bands || []).map(b => (b[0]*100).toFixed(0) + '~' + (b[1]*100).toFixed(0) + '%').join(', ') }}</span>
        <span class="muted">事件 {{ c.event_hz ? c.event_hz.toFixed(3) : '—' }}/s</span>
        <span class="muted">人声 {{ c.speech_ratio == null ? '—' : (c.speech_ratio*100).toFixed(0) + '%' }}</span>
        <span class="muted" style="font-size:12px">{{ c.duration ? c.duration + 's' : '' }}</span>
        <button class="ghost" :disabled="busy" @click="clearCache(c)">清除</button>
      </div>
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
        <label>视频/页面里的广告信息
          <select v-model="form.ads_policy"
                  style="padding:6px 8px;border:1px solid var(--input-border);border-radius:6px">
            <option value="ignore">忽略(不写进 AI 属性,保持内容干净)</option>
            <option value="mention">说明(单独一条「推广信息」属性)</option>
          </select>
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
            <td>
              <b>主路径 · 结构单元化</b>:{{ s.frames.unit === 'off' ?
                '已关闭(全部回退到均匀抽帧)' :
                'auto(画像判定「字幕为主」的视频:按动作事件/成品展示落库,' +
                 '帧数 = 单元数 × 2;单元数上限 ' +
                 (s.frames.max_units || '无') + ')' }}<br>
              <b>兜底路径 · 均匀抽帧</b>(未命中单元化时用:旁白类视频、探针判不出、
              unit=off 回退;上限 {{ s.frames.max_frames }} 帧,间隔 {{ s.frames.frame_interval }}s、
              场景阈值 {{ s.frames.scene_threshold }})
              <span class="muted">— 上限只限数量:间隔会自动放大,帧仍铺满全片</span><br>
              <span class="muted">本库用法:结构单元化 {{ s.usage?.unitized ?? 0 }} 条视频 /
                均匀抽帧 {{ s.usage?.uniform ?? 0 }} 条(共 {{ s.usage?.videos ?? 0 }} 条)</span><br>
              <span class="muted">品类(分类树)本应只用于选「先验配置」;该预置目前未做,
                实际只有画像这一条判据(未命中 → 纯预处理兜底)</span><br>
              <span class="muted">解码:{{ s.frames.decode === 'seek' ? '逐点定位(慢)' : '顺序解码(快)' }} ·
              画像探针:{{ s.frames.probe?.enabled }} · {{ s.frames.probe?.hz }}Hz ·
              {{ s.frames.probe?.bands }} 条带 · 分离度 ≥{{ s.frames.probe?.min_separation }}×</span>
            </td></tr>
        <tr><th>OCR</th>
            <td>{{ s.vision.ocr?.enabled }} · 人声占比低于 {{ s.vision.ocr?.speech_ratio }} 才跑<br>
              <span class="muted">双通道:字幕带
                {{ s.vision.ocr?.band === 'off' ? '关闭(整幅识别,会带水印)' :
                   'auto(裁字幕带,水印不进文本)' }} ·
                每 {{ s.vision.ocr?.full_every }} 帧补一次整幅(卖点/参数文字)</span>
            </td></tr>
        <tr><th>图像嵌入</th>
            <td>{{ s.vision.image_embed?.enabled }}(Chinese-CLIP,缺权重自动跳过)</td></tr>
        <tr><th>弹幕</th>
            <td>{{ s.vision.danmaku?.enabled }} ·
              {{ s.vision.danmaku?.window }}s 窗内 ≥{{ s.vision.danmaku?.min_hits }} 条广告词才算广告段</td></tr>
        <tr><th>关键片段</th>
            <td>边界{{ s.clips?.snap === 'off' ? '用模型原始值' : '吸附到结构单元' }}</td></tr>
        <tr><th>双链</th>
            <td>阈值 {{ s.links.similarity_threshold }} · 每条最多 {{ s.links.max_per_item }} 条</td></tr>
        <tr><th>广告处理</th>
            <td>{{ s.llm.ads_policy === 'mention' ? '单独列为「推广信息」属性' : '忽略(不写进属性)' }}</td></tr>
        <tr><th>文本嵌入</th>
            <td>{{ s.embedding.text_model }}{{ s.embedding.fake ? '(fake 模式)' : '' }}</td></tr>
      </table>
      <p class="muted" style="margin:8px 0 0">
        这些值来自 <b>config.yaml</b>(改文件后重启生效);上面表单只覆盖 LLM 相关项。</p>
    </div>
  </div>
</template>
