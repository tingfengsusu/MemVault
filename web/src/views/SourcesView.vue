<script setup>
/**
 * 订阅源(Vue 版):新增 B站 UP主订阅 + 列表(启停/立即检查)。
 * 数据:GET /api/sources;动作:POST /api/sources[/{id}/toggle|/check]
 */
import { onMounted, ref } from 'vue'
import ToastHost from '../components/ToastHost.vue'
import { showToast } from '../lib/toast.js'
import { ApiError, api, sourceApi } from '../api/client.js'

const sources = ref([])
const loading = ref(true)
const busy = ref(false)
const form = ref({ kind: 'bili_up', target: '', domain: 'general' })

const flash = (kind, text) => showToast(kind, text)

async function load() {
  loading.value = true
  try {
    const d = await api.get('/api/sources')
    sources.value = d.items
  } catch (e) {
    flash('err', e instanceof ApiError ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

async function add() {
  if (!form.value.target.trim()) return flash('err', '请填 UP主 UID 或 space 链接')
  busy.value = true
  try {
    const s = await sourceApi.add(form.value.kind, form.value.target.trim(),
                                  form.value.domain.trim() || 'general')
    flash('ok', `已订阅 ${s.label || s.target}(UID ${s.target})→ 领域 ${s.domain}`)
    form.value.target = ''
    await load()
  } catch (e) {
    flash('err', e.message)
  } finally {
    busy.value = false
  }
}

async function toggle(s) {
  busy.value = true
  try {
    const d = await sourceApi.toggle(s.id)
    flash('ok', `${d.label || d.target}:已${d.enabled ? '启用' : '停用'}`)
    await load()
  } catch (e) {
    flash('err', e.message)
  } finally {
    busy.value = false
  }
}

async function check(s) {
  busy.value = true
  try {
    await sourceApi.check(s.id)
    flash('ok', '已排入检查队列(任务页可看进度;首次检查只登记历史)')
    await load()
  } catch (e) {
    flash('err', e.message)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <h3 style="margin:4px 0 12px">订阅源</h3>
    <ToastHost />

    <div class="card">
      <form style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"
            @submit.prevent="add">
        <select v-model="form.kind" style="padding:6px 8px;border:1px solid var(--input-border);border-radius:6px">
          <option value="bili_up">B站 UP主</option>
        </select>
        <input v-model="form.target" placeholder="UP主 UID 或 space.bilibili.com/xxx" required
               style="padding:6px 10px;border:1px solid var(--input-border);border-radius:6px;width:240px">
        <input v-model="form.domain" placeholder="入库领域,如 fitness"
               style="padding:6px 10px;border:1px solid var(--input-border);border-radius:6px;width:140px">
        <button :disabled="busy">添加订阅</button>
        <span class="muted">首次检查只登记历史;此后每 30 分钟自动拉新。B站风控限制,需在
          <b>config.yaml</b> 的 <b>bili.cookies_path</b> 配置导出的 cookies.txt(登录态)。</span>
      </form>
    </div>

    <div class="card">
      <table>
        <tr>
          <th>#</th><th>类型</th><th>订阅对象</th><th>领域</th>
          <th>状态</th><th>上次检查</th><th></th>
        </tr>
        <tr v-for="s in sources" :key="s.id">
          <td>{{ s.id }}</td>
          <td>{{ s.kind }}</td>
          <td>{{ s.label || s.target }} <span class="muted">UID {{ s.target }}</span></td>
          <td><span class="tag">{{ s.domain || 'general' }}</span></td>
          <td>
            <span v-if="s.enabled" class="tag" style="background:#ecfdf5;color:#047857">启用</span>
            <span v-else class="tag gray">停用</span>
          </td>
          <td class="muted">{{ s.last_checked || '从未' }}</td>
          <td style="display:flex;gap:6px">
            <button class="ghost" :disabled="busy" @click="check(s)">立即检查</button>
            <button class="ghost" :disabled="busy" @click="toggle(s)">
              {{ s.enabled ? '停用' : '启用' }}</button>
          </td>
        </tr>
        <tr v-if="!loading && !sources.length">
          <td colspan="7" class="muted">还没有订阅源。添加 UP主 后,新视频会自动采集入库并走自动分类。</td>
        </tr>
      </table>
    </div>
  </div>
</template>
