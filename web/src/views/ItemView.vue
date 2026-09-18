<script setup>
/**
 * 条目详情(Vue 版):AI 提取 / 原始属性 / 语义块时间轴 / 相关条目 /
 * UP主绑定 / 重新分析 / 加购。
 * 数据:/api/items/{id} + /api/categories;动作:/api/items/{id}/*
 */
import { computed, onMounted, ref } from 'vue'
import { ApiError, api, itemApi } from '../api/client.js'

const props = defineProps({
  itemId: { type: [Number, String], required: true },
})
const item = ref(null)
const categories = ref([])
const loading = ref(true)
const busy = ref(false)
const toast = ref(null)
const pickCategory = ref('')
const reloading = ref(false)

const icon = { video: '🎬', product: '🛒', doc: '📄', note: '📝', image: '🖼', file: '📎' }
const aiAttrs = computed(() => Object.entries(item.value?.attrs_ai || {}))
const rawAttrs = computed(() => Object.entries(item.value?.attrs_raw || {}))
const textChunks = computed(() =>
  (item.value?.chunks || []).filter(c => c.modality === 'text'))
const imageChunks = computed(() =>
  (item.value?.chunks || []).filter(c => c.modality === 'image'))

function flash(kind, text) {
  toast.value = { kind, text }
  setTimeout(() => { if (toast.value?.text === text) toast.value = null }, 5000)
}

async function load(showSpinner = true) {
  if (showSpinner) loading.value = true
  try {
    item.value = await api.get(`/api/items/${props.itemId}`)
    const cats = await api.get('/api/categories')
    categories.value = cats.items || []
    const cur = item.value.up?.rule_category_id
    pickCategory.value = cur || item.value.category_id || categories.value[0]?.id || ''
  } catch (e) {
    flash('err', e instanceof ApiError ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

async function act(fn, okText) {
  busy.value = true
  try {
    await fn()
    flash('ok', okText)
    await load(false)
  } catch (e) {
    flash('err', e.message)
  } finally {
    busy.value = false
  }
}

const reanalyze = () => act(() => itemApi.reanalyze(item.value.id),
  '已排入 AI 重新分析队列(任务页可看进度)')
const addToCart = () => act(() => itemApi.cart(item.value.id), '已排入京东加购队列')
const bindUp = () => act(
  () => itemApi.bindUp(item.value.id, pickCategory.value,
                       item.value.up?.up_mid, item.value.up?.up_name),
  '已把该 UP 的视频归到此分类')
const unbindUp = () => act(() => itemApi.unbindUp(item.value.up.up_mid),
  '已解除该 UP 的分类规则')
const setStatus = (s) => act(() => itemApi.setStatus(item.value.id, s),
  s === 'archived' ? '已归档' : '状态已更新')

onMounted(() => load())
</script>

<template>
  <div>
    <div v-if="loading" class="card"><span class="muted">加载中…</span></div>

    <div v-else-if="!item" class="card empty">
      <h3>条目不存在</h3>
      <p class="muted">可能已被删除,或链接里的 id 不对。</p>
    </div>

    <template v-else>
      <div v-if="toast" class="card">
        <span :class="toast.kind === 'err' ? 'tag warn' : 'tag'">
          {{ toast.kind === 'err' ? '出错' : '完成' }}</span>
        <span class="muted" style="margin-left:8px">{{ toast.text }}</span>
      </div>

      <div class="card">
        <h3 style="font-size:17px">
          <span>{{ icon[item.type] || '📄' }}</span> {{ item.title }}
        </h3>
        <div style="margin:6px 0">
          <span class="tag">{{ item.domain }}</span>
          <span class="tag gray">{{ item.type }}</span>
          <span class="tag gray">{{ item.status }}</span>
          <span v-if="item.category_name" class="tag">分类:{{ item.category_name }}</span>
          <span v-if="item.category_conf !== null && item.category_conf !== undefined"
                class="tag gray">置信度 {{ Number(item.category_conf).toFixed(2) }}</span>
          <span class="muted">{{ item.created_at }} · {{ item.chunk_count }} 个语义块</span>
        </div>
        <p v-if="item.source_ref" class="muted" style="word-break:break-all;margin:4px 0">
          来源:
          <a v-if="String(item.source_ref).startsWith('http')" class="jump"
             :href="item.source_ref" target="_blank">{{ item.source_ref }}</a>
          <template v-else>{{ item.source_ref }}</template>
        </p>

        <template v-if="aiAttrs.length">
          <p class="muted" style="margin:10px 0 2px">AI 提取(重新分析会整体替换)</p>
          <table>
            <tr v-for="[k, v] in aiAttrs" :key="k">
              <th style="width:110px">{{ k }}</th><td>{{ v }}</td>
            </tr>
          </table>
        </template>
        <template v-if="rawAttrs.length">
          <p class="muted" style="margin:10px 0 2px">原始属性(采集时记录)</p>
          <table>
            <tr v-for="[k, v] in rawAttrs" :key="k">
              <th style="width:110px">{{ k }}</th><td>{{ v }}</td>
            </tr>
          </table>
        </template>
        <p v-if="item.auto_note" class="muted" style="margin-top:8px">
          自动分析:{{ item.auto_note }}</p>

        <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
          <button class="ghost" :disabled="busy" @click="reanalyze">🔄 重新分析</button>
          <button v-if="item.type === 'product'" :disabled="busy" @click="addToCart">
            🛒 加入京东购物车</button>
          <button class="ghost" :disabled="busy" @click="setStatus('inbox')">放回待整理箱</button>
          <button class="ghost" :disabled="busy" @click="setStatus('archived')">归档</button>
          <a class="jump" :href="`/search?similar=${item.id}:${imageChunks[0]?.id}`"
             v-if="imageChunks.length">🔍 以首个画面找相似</a>
        </div>

        <div v-if="item.up && item.up.up_mid"
             style="margin-top:10px;padding-top:8px;border-top:1px dashed var(--card-border)">
          <p class="muted" style="margin:0 0 4px">
            UP主:{{ item.up.up_name || ('mid ' + item.up.up_mid) }}
            <template v-if="item.up.rule_category_id">
              · 已绑定规则 → 分类 #{{ item.up.rule_category_id }}
            </template>
          </p>
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
            <select v-model="pickCategory">
              <option v-for="c in categories" :key="c.id" :value="c.id">
                {{ c.domain }} / {{ c.name }}<template v-if="c.status === 'proposed'">(待确认)</template>
              </option>
            </select>
            <button class="ghost" :disabled="busy" @click="bindUp">📌 该 UP 的视频都归此分类</button>
            <button v-if="item.up.rule_category_id" class="ghost" :disabled="busy"
                    @click="unbindUp">解除该 UP 的分类规则</button>
          </div>
        </div>
      </div>

      <div v-if="item.related.length" class="card">
        <p class="muted" style="margin:0 0 6px">🔗 相关条目(向量相似度自动链接)</p>
        <div v-for="r in item.related" :key="r.id"
             style="display:flex;gap:10px;align-items:center;padding:5px 0;border-bottom:1px dashed var(--card-border)">
          <a class="jump" :href="`/items/${r.id}`" style="font-size:13px">{{ r.title }}</a>
          <span class="muted">相似度 {{ Number(r.score).toFixed(2) }}</span>
        </div>
      </div>

      <h3 style="margin:18px 0 10px">语义块({{ item.chunks.length }})</h3>
      <div class="card">
        <div v-for="c in item.chunks" :key="c.id" class="chunk">
          <div class="chunk-head">
            <span class="tag gray">{{ c.modality }}</span>
            <span v-if="c.timestamp_label" class="ts">{{ c.timestamp_label }}</span>
            <a v-if="c.jump" class="jump" :href="c.jump" target="_blank">▶ 跳到视频此处</a>
            <span class="muted">#{{ c.seq }}</span>
            <span v-if="c.embed_status !== 'done'" class="tag warn">{{ c.embed_status }}</span>
          </div>
          <div v-if="c.content" class="snippet full">{{ c.content.slice(0, 600) }}</div>
          <template v-if="c.media_url">
            <img class="thumb" :src="c.media_url" loading="lazy">
            <a class="jump" style="font-size:12px"
               :href="`/search?similar=${item.id}:${c.id}`">🔍 找相似画面</a>
          </template>
        </div>
      </div>
    </template>
  </div>
</template>
