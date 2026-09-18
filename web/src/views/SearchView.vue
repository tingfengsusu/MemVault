<script setup>
/**
 * 检索页(Vue 版):文本混合检索 + 相关画面(文字搜画面)+ 以图搜图上传/拖拽。
 * 数据走 /api/items、/api/search/images、/api/search/image;样式沿用面板既有 class。
 */
import { computed, onMounted, ref } from 'vue'
import ToastHost from '../components/ToastHost.vue'
import { showToast } from '../lib/toast.js'
import { ApiError, api, searchApi } from '../api/client.js'

const props = defineProps({
  initialQuery: { type: String, default: '' },
  similar: { type: String, default: '' },     // "itemId:chunkId" —— 从条目页跳来的找相似画面
})

const q = ref(props.initialQuery || '')
const domain = ref('')
const results = ref([])          // 文本命中(含 hit_chunk)
const images = ref([])           // 画面命中
const total = ref(0)
const page = ref(1)
const pageSize = 10
const loading = ref(false)
const searching = ref(false)     // 正在检索(用于"思考中"提示,避免等待被主观放大)
const queryImage = ref('')       // 以图搜图时回显的查询图
const queryLabel = ref('')
const dragging = ref(false)
const fileInput = ref(null)

const domains = ref([])
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const imageSearchOn = ref(true)

const flash = (kind, text) => showToast(kind, text)

async function loadDomains() {
  try {
    const d = await api.get('/api/stats')
    domains.value = d.domains || []
  } catch { /* 领域条是增强项,失败就只显示"全部" */ }
}

async function doSearch(toPage = 1) {
  if (!q.value.trim()) { results.value = []; images.value = []; total.value = 0; return }
  searching.value = true
  page.value = toPage
  queryImage.value = ''
  queryLabel.value = ''
  try {
    const d = await searchApi.search({ q: q.value, domain: domain.value,
                                       page: toPage, pageSize })
    results.value = d.items
    total.value = d.total
    const im = await api.get('/api/search/images', { q: q.value, domain: domain.value })
    images.value = im.items
    imageSearchOn.value = im.enabled
  } catch (e) {
    flash('err', e instanceof ApiError ? e.message : String(e))
  } finally {
    searching.value = false
  }
}

async function uploadImage(file) {
  if (!file) return
  loading.value = true
  try {
    const d = await searchApi.uploadImage(file)
    images.value = d.items
    results.value = []
    total.value = 0
    queryImage.value = d.query_image
    queryLabel.value = d.query_label
    q.value = ''
  } catch (e) {
    flash('err', e instanceof ApiError ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

function onPick(e) {
  uploadImage(e.target.files?.[0])
  e.target.value = ''
}

function onDrop(e) {
  dragging.value = false
  const f = e.dataTransfer?.files?.[0]
  if (f && f.type.startsWith('image/')) uploadImage(f)
  else flash('err', '请拖入一张图片')
}

function fmtTs(sec) {
  if (sec === null || sec === undefined) return ''
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60)
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function jumpUrl(it, chunk) {
  if (chunk?.source_ref && chunk.start_ts !== null && chunk.start_ts !== undefined
      && String(chunk.source_ref).includes('BV')) {
    return `https://www.bilibili.com/video/${chunk.source_ref}?t=${Math.floor(chunk.start_ts)}`
  }
  return ''
}

/** 库里已有帧的"找相似画面"(条目详情页跳来:/search?similar=itemId:chunkId) */
async function findSimilar(spec) {
  const [itemId, chunkId] = String(spec).split(':').map(Number)
  if (!itemId || !chunkId) return
  loading.value = true
  try {
    const d = await searchApi.similar(itemId, chunkId)
    images.value = d.items
    results.value = []
    total.value = 0
    queryImage.value = d.query_image
    queryLabel.value = d.query_label
  } catch (e) {
    flash('err', e instanceof ApiError ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadDomains()
  if (props.similar) findSimilar(props.similar)
  else if (q.value) doSearch(1)
})
</script>

<template>
  <div @dragover.prevent="dragging = true" @dragleave="dragging = false" @drop.prevent="onDrop">
    <h3 style="margin:4px 0 12px">检索</h3>
    <ToastHost />

    <div class="card" :style="dragging ? { borderColor: 'var(--link, #4c6ef5)' } : {}">
      <form style="display:flex;gap:8px;align-items:center;flex-wrap:wrap" @submit.prevent="doSearch(1)">
        <input v-model="q" placeholder="关键词 / 语义描述,如:冰淇淋 打发"
               style="flex:1;min-width:220px">
        <select v-model="domain">
          <option value="">全部领域</option>
          <option v-for="d in domains" :key="d.domain" :value="d.domain">
            {{ d.domain }}({{ d.c }})
          </option>
        </select>
        <button :disabled="searching">{{ searching ? '检索中…' : '搜索' }}</button>
      </form>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px">
        <span class="muted">以图搜图:</span>
        <input ref="fileInput" type="file" accept="image/*" style="font-size:13px" @change="onPick">
        <span class="muted" style="font-size:12px">(或把图片拖到本页)</span>
        <span v-if="!imageSearchOn" class="tag warn">图像检索未启用(cn-clip 权重缺失)</span>
      </div>
    </div>

    <div v-if="queryLabel" class="card" style="display:flex;gap:12px;align-items:center">
      <img v-if="queryImage" :src="queryImage" style="height:72px;border-radius:6px">
      <div>
        <h3 style="margin:0">以图搜图:{{ queryLabel }}</h3>
        <p class="muted" style="margin:4px 0 0">共 {{ images.length }} 个相似画面</p>
      </div>
    </div>

    <h3 v-if="q" style="margin:8px 0 10px">
      “{{ q }}” 的结果({{ total }}{{ images.length ? ` + 画面 ${images.length}` : '' }})
      <span v-if="searching" class="muted" style="font-weight:400;font-size:13px">检索中…</span>
    </h3>

    <div v-if="images.length" class="card">
      <p class="muted" style="margin:0 0 8px">🖼 相关画面(Chinese-CLIP 向量,按相似度排序)</p>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:10px">
        <a v-for="h in images" :key="h.chunk_id" :href="`/items/${h.item.id}`"
           style="text-decoration:none;color:inherit">
          <img v-if="h.media_url" :src="h.media_url" loading="lazy"
               style="width:100%;height:96px;object-fit:cover;border-radius:6px;border:1px solid var(--card-border)">
          <div class="muted" style="font-size:12px;margin-top:4px">
            #{{ h.item.id }} {{ h.item.title.slice(0, 16) }}
            <template v-if="h.start_ts !== null && h.start_ts !== undefined">· {{ fmtTs(h.start_ts) }}</template>
            · {{ h.score.toFixed(2) }}
          </div>
        </a>
      </div>
    </div>

    <div v-for="(r, i) in results" :key="r.id" class="card">
      <h3><a :href="`/items/${r.id}`">{{ r.title }}</a></h3>
      <div>
        <span class="tag">{{ r.domain }}</span>
        <span class="tag gray">{{ r.type }}</span>
        <span class="tag gray">第 {{ (page - 1) * pageSize + i + 1 }} 条命中</span>
        <span v-if="r.chunk_count" class="muted">{{ r.chunk_count }} 个语义块</span>
        <template v-if="r.hit_chunk">
          <span v-if="r.hit_chunk.start_ts !== null && r.hit_chunk.start_ts !== undefined"
                class="ts">{{ fmtTs(r.hit_chunk.start_ts) }}</span>
          <a v-if="jumpUrl(r, r.hit_chunk)" class="jump" :href="jumpUrl(r, r.hit_chunk)"
             target="_blank">▶ 跳到视频此处</a>
        </template>
      </div>
      <div v-if="r.hit_chunk?.content" class="snippet">{{ r.hit_chunk.content }}</div>
      <div v-else-if="r.snippet" class="snippet">{{ r.snippet }}…</div>
      <img v-if="r.hit_chunk?.media_url" class="thumb" :src="r.hit_chunk.media_url" loading="lazy">
      <img v-else-if="r.thumb" class="thumb" :src="r.thumb" loading="lazy">
    </div>

    <div v-if="pageCount > 1" class="chips" style="justify-content:center">
      <a v-if="page > 1" class="jump" href="#" @click.prevent="doSearch(page - 1)">← 上一页</a>
      <span class="muted">第 {{ page }} / {{ pageCount }} 页</span>
      <a v-if="page < pageCount" class="jump" href="#" @click.prevent="doSearch(page + 1)">下一页 →</a>
    </div>

    <div v-if="q && !searching && !results.length && !images.length" class="card empty">
      <h3>无结果</h3>
      <p class="muted">换个关键词试试;混合检索 = 向量语义 + 关键词,画面命中靠 Chinese-CLIP。</p>
    </div>
  </div>
</template>
