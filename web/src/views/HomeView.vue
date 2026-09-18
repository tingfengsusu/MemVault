<script setup>
/**
 * 库首页(Vue 版):统计条 + 领域筛选 + 条目卡片(缩略图/摘要/相关条目)+ 分页。
 * 数据:/api/stats、/api/items?with_related=1
 */
import { computed, onMounted, ref } from 'vue'
import ToastHost from '../components/ToastHost.vue'
import { showToast } from '../lib/toast.js'
import { ApiError, api } from '../api/client.js'

const props = defineProps({
  initialDomain: { type: String, default: '' },
})

const stats = ref({})
const domains = ref([])
const items = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 50
const domain = ref(props.initialDomain || '')
const loading = ref(true)

const icons = { video: '🎬', product: '🛒', doc: '📄', note: '📝', image: '🖼', file: '📎' }
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

const flash = (kind, text) => showToast(kind, text)

async function load(toPage = 1) {
  loading.value = true
  page.value = toPage
  try {
    const [s, d] = await Promise.all([
      api.get('/api/stats'),
      api.get('/api/items', { domain: domain.value, page: toPage,
                              page_size: pageSize, with_related: 1 }),
    ])
    stats.value = s
    domains.value = s.domains || []
    items.value = d.items
    total.value = d.total
  } catch (e) {
    flash('err', e instanceof ApiError ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

function pick(d) {
  domain.value = d
  load(1)
}

onMounted(() => load(1))
</script>

<template>
  <div>
    <ToastHost />

    <div class="chips">
      <span>条目 <b>{{ stats.items ?? '—' }}</b></span>
      <span>语义块 <b>{{ stats.chunks ?? '—' }}</b></span>
      <span>待整理 <b>{{ stats.inbox ?? '—' }}</b></span>
      <span>日志 <b>{{ stats.logs ?? '—' }}</b></span>
      <span v-if="stats.pending_images" class="muted">
        ({{ stats.pending_images }} 个画面待建向量)</span>
    </div>

    <div class="chips">
      <span>领域:</span>
      <a class="tag" :class="{ gray: domain }" href="#" @click.prevent="pick('')">全部</a>
      <a v-for="d in domains" :key="d.domain" class="tag"
         :class="{ gray: domain !== d.domain }" href="#"
         @click.prevent="pick(d.domain)">{{ d.domain }}({{ d.c }})</a>
      <span v-if="loading" class="muted" style="margin-left:8px">加载中…</span>
    </div>

    <div class="grid">
      <div v-for="it in items" :key="it.id" class="card tile">
        <a v-if="it.thumb" :href="`/items/${it.id}`">
          <img class="thumb-top" :src="it.thumb" loading="lazy" alt="">
        </a>
        <div class="tile-body">
          <h3>
            <span>{{ icons[it.type] || '📄' }}</span>
            <a class="stretch" :href="`/items/${it.id}`">{{ it.title }}</a>
          </h3>
          <div class="meta">
            <span class="dot" :class="`dot-${it.domain}`" :title="it.domain"></span>
            <span class="muted">{{ it.domain }}</span>
            <span class="tag gray">{{ it.type }}</span>
            <span v-if="it.chunk_count" class="muted">{{ it.chunk_count }} 个语义块</span>
            <span v-if="it.category_id" class="tag">已分类</span>
          </div>
          <div v-if="it.snippet" class="snippet">{{ it.snippet }}</div>
          <div v-if="it.related && it.related.length" class="rel muted">
            🔗 相关:
            <template v-for="(r, i) in it.related" :key="r.id">
              <a :href="`/items/${r.id}`">{{ r.title.slice(0, 20) }}</a>{{ i < it.related.length - 1 ? ' · ' : '' }}
            </template>
          </div>
        </div>
      </div>

      <div v-if="!loading && !items.length" class="card empty" style="grid-column:1/-1">
        <h3>库还是空的</h3>
        <p class="muted">用浏览器插件采集网页/商品/视频,或按全局热键 <b>Ctrl+Alt+B</b>
          采集剪贴板内容(支持文件)。</p>
      </div>
    </div>

    <div v-if="pageCount > 1" class="chips" style="justify-content:center">
      <a v-if="page > 1" class="jump" href="#" @click.prevent="load(page - 1)">← 上一页</a>
      <span class="muted">第 {{ page }} / {{ pageCount }} 页</span>
      <a v-if="page < pageCount" class="jump" href="#" @click.prevent="load(page + 1)">下一页 →</a>
    </div>
  </div>
</template>
