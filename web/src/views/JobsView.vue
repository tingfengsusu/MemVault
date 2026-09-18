<script setup>
/**
 * 任务队列(Vue 版):只读列表 + 状态筛选(默认全部,可只看失败/进行中)。
 * 数据:GET /api/jobs
 */
import { computed, onMounted, ref } from 'vue'
import ToastHost from '../components/ToastHost.vue'
import { showToast } from '../lib/toast.js'
import { ApiError, api } from '../api/client.js'

const jobs = ref([])
const filter = ref('')
const loading = ref(true)

const shown = computed(() => filter.value
  ? jobs.value.filter(j => j.status === filter.value)
  : jobs.value)
const counts = computed(() => jobs.value.reduce((acc, j) => {
  acc[j.status] = (acc[j.status] || 0) + 1
  return acc
}, {}))

async function load() {
  loading.value = true
  try {
    const d = await api.get('/api/jobs', { limit: 50 })
    jobs.value = d.items
  } catch (e) {
    flash('err', e instanceof ApiError ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

function badge(j) {
  if (j.status === 'done') return { cls: 'tag ok', text: `✔ 完成${j.duration ? ' · 耗时 ' + j.duration : ''}` }
  if (j.status === 'failed') return { cls: 'tag bad', text: `✘ 失败${j.duration ? ' · 耗时 ' + j.duration : ''}` }
  if (j.status === 'running') return { cls: 'tag ok', text: '⚙ 处理中' }
  return { cls: 'tag gray', text: '⏳ 排队中' }
}

onMounted(load)
</script>

<template>
  <div>
    <h3 style="margin:4px 0 12px">
      任务队列(最近 50)
      <span class="muted" style="font-weight:400;font-size:13px">
        <a href="#" class="jump" @click.prevent="load">{{ loading ? '刷新中…' : '刷新' }}</a>
      </span>
    </h3>

    <div class="chips">
      <span>状态:</span>
      <a class="tag" :class="{ gray: filter }" href="#" @click.prevent="filter = ''">
        全部({{ jobs.length }})</a>
      <a v-for="s in ['failed', 'running', 'pending', 'done']" :key="s" class="tag"
         :class="{ gray: filter !== s }" href="#" @click.prevent="filter = s">
        {{ { failed: '失败', running: '处理中', pending: '排队中', done: '完成' }[s] }}({{ counts[s] || 0 }})
      </a>
    </div>
    <ToastHost />

    <div v-for="j in shown" :key="j.id" class="card">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <b>#{{ j.id }} {{ j.type }}</b>
        <span :class="badge(j).cls">{{ badge(j).text }}</span>
        <span class="muted">{{ j.created_at }}</span>
      </div>
      <div class="snippet">处理内容:{{ j.payload_pretty }}</div>
      <div v-if="j.error" class="snippet" style="color:#b91c1c">错误:{{ j.error.slice(0, 300) }}</div>
    </div>

    <div v-if="!loading && !shown.length" class="card empty">
      <h3>{{ filter ? '该状态下暂无任务' : '暂无任务' }}</h3>
      <p class="muted">采集、自动分类、订阅检查、京东加购都会在这里留下记录,含处理内容与结果。</p>
    </div>
  </div>
</template>
