<script setup>
/**
 * 待整理箱(Vue 版)。
 * 数据全部走 /api/inbox 与 /api/items/* 的 JSON 契约;
 * 样式沿用面板既有 class(card/tag/muted/chips…),因此三个主题(theme.css)照旧生效。
 */
import { computed, onMounted, ref } from 'vue'
import ToastHost from '../components/ToastHost.vue'
import { showToast } from '../lib/toast.js'
import { ApiError, inboxApi } from '../api/client.js'

const props = defineProps({
  initialDomain: { type: String, default: '' },
})

const items = ref([])
const categories = ref({})        // {domain: [{id,name,...}]}
const domainCounts = ref([])      // 待整理箱的领域分布
const total = ref(0)
const domain = ref(props.initialDomain || '')
const loading = ref(false)
const busy = ref(false)
const toast = ref(null)           // {kind:'ok'|'err', text}
const selected = ref(new Set())
const pickCategory = ref({})      // itemId -> categoryId

const allSelected = computed(
  () => items.value.length > 0 && selected.value.size === items.value.length,
)
const selectedIds = computed(() => [...selected.value])
const scopeLabel = computed(() => (domain.value ? `${domain.value} 领域` : '全部领域'))

const flash = (kind, text) => showToast(kind, text)

async function load() {
  loading.value = true
  try {
    const d = await inboxApi.load({ domain: domain.value })
    items.value = d.items
    total.value = d.total
    categories.value = d.categories || {}
    domainCounts.value = d.domain_counts || []
    selected.value = new Set()
    for (const it of d.items) {
      const cats = categories.value[it.domain] || []
      pickCategory.value[it.id] = it.category_id ?? cats[0]?.id ?? ''
    }
  } catch (e) {
    flash('err', e instanceof ApiError ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

function toggle(id) {
  const s = new Set(selected.value)
  s.has(id) ? s.delete(id) : s.add(id)
  selected.value = s
}

function toggleAll() {
  selected.value = allSelected.value ? new Set() : new Set(items.value.map(i => i.id))
}

async function runBatch(action) {
  const ids = selectedIds.value
  if (action === 'auto') {
    const n = ids.length || total.value
    if (!chrome_confirm(`将对 ${n} 条逐条调用 AI 自动分类(消耗额度,约需几分钟),继续?`)) return
  }
  busy.value = true
  try {
    const d = await inboxApi.batch({ action, ids, domain: domain.value })
    flash('ok', `已处理 ${d.affected} 条（${action}）`)
    await load()
  } catch (e) {
    flash('err', e.message)
  } finally {
    busy.value = false
  }
}

async function act(item, fn, okText) {
  busy.value = true
  try {
    await fn()
    flash('ok', okText)
    await load()
  } catch (e) {
    flash('err', e.message)
  } finally {
    busy.value = false
  }
}

const classify = (it) => act(it, () => inboxApi.classify(it.id, pickCategory.value[it.id]),
  `已归入:${it.title.slice(0, 16)}`)
const setStatus = (it, status) => act(it, () => inboxApi.setStatus(it.id, status),
  status === 'archived' ? '已归档' : '已归类')
const reanalyze = (it) => act(it, () => inboxApi.reanalyze(it.id), '已排入 AI 重新分析队列')

function chrome_confirm(msg) {
  return window.confirm(msg)
}

onMounted(load)
</script>

<template>
  <div>
    <h3 style="margin:4px 0 12px">
      待整理箱({{ total }})
      <span v-if="loading" class="muted" style="font-weight:400;font-size:13px">加载中…</span>
    </h3>
    <ToastHost />

    <div class="card">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <span class="muted">批量操作({{ scopeLabel }}{{ selectedIds.length ? ` · 已选 ${selectedIds.length}` : '' }}):</span>
        <button :disabled="busy" @click="runBatch('filed')">✔ 全部已归类</button>
        <button class="ghost" :disabled="busy" @click="runBatch('archived')">🗃 全部归档</button>
        <button class="ghost" :disabled="busy" @click="runBatch('auto')">🤖 全部交给 AI 分类</button>
        <span class="muted">「已归类」=确认处理完毕;「归档」=不再显示但保留数据。</span>
      </div>
      <div class="chips" style="margin:8px 0 0">
        <span>按领域:</span>
        <a class="tag" :class="{ gray: domain }" href="#" @click.prevent="domain = ''; load()">全部</a>
        <a v-for="d in domainCounts" :key="d.domain" class="tag"
           :class="{ gray: domain !== d.domain }" href="#"
           @click.prevent="domain = d.domain; load()">
          {{ d.domain }}({{ d.c }})
        </a>
        <span v-if="items.length" class="muted" style="margin-left:8px">
          <a href="#" class="jump" @click.prevent="toggleAll()">
            {{ allSelected ? '取消全选' : '全选' }}</a>
        </span>
      </div>
    </div>

    <div v-for="it in items" :key="it.id" class="card">
      <h3 style="display:flex;align-items:center;gap:8px">
        <input type="checkbox" :checked="selected.has(it.id)" @change="toggle(it.id)">
        <img v-if="it.thumb" :src="it.thumb" style="height:34px;border-radius:4px">
        <a :href="`/items/${it.id}`">{{ it.title }}</a>
      </h3>
      <div>
        <span class="tag">{{ it.domain }}</span>
        <span class="tag gray">{{ it.type }}</span>
        <span v-if="it.chunk_count" class="muted">{{ it.chunk_count }} 个语义块</span>
        <span v-if="it.category_id && it.category_name" class="tag warn">
          AI 建议:{{ it.category_name }}</span>
      </div>
      <div v-if="it.snippet" class="snippet">{{ it.snippet }}…</div>
      <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <template v-if="(categories[it.domain] || []).length">
          <select v-model="pickCategory[it.id]">
            <option v-for="c in categories[it.domain]" :key="c.id" :value="c.id">
              {{ c.name }}<template v-if="c.status === 'proposed'">(待确认)</template>
            </option>
          </select>
          <button :disabled="busy" @click="classify(it)">归入所选分类</button>
        </template>
        <button class="ghost" :disabled="busy" @click="setStatus(it, 'filed')">✔ 已归类(不指定)</button>
        <button class="ghost" :disabled="busy" @click="setStatus(it, 'archived')">归档</button>
        <button class="ghost" :disabled="busy" @click="reanalyze(it)">🔄 重新分析</button>
        <a class="jump" :href="`/items/${it.id}`">打开详情 →</a>
      </div>
    </div>

    <div v-if="!loading && !items.length" class="card empty">
      <h3>空空如也</h3>
      <p class="muted">新采集的条目会先落在这里;开启 LLM 后自动分类会定期消化它们。</p>
    </div>
  </div>
</template>
