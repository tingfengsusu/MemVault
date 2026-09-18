<script setup>
/**
 * 分类管理(Vue 版):新增分类(领域带下拉建议)+ 分类树(确认采纳/删除)+ UP主规则。
 * 数据:/api/categories;动作:POST /api/categories[/confirm|/delete]、/api/up/{mid}/unbind
 */
import { onMounted, ref } from 'vue'
import { ApiError, api, categoryApi } from '../api/client.js'

const groups = ref({})
const domains = ref([])
const upRules = ref([])
const loading = ref(true)
const busy = ref(false)
const toast = ref(null)
const form = ref({ domain: '', name: '' })

function flash(kind, text) {
  toast.value = { kind, text }
  setTimeout(() => { if (toast.value?.text === text) toast.value = null }, 5000)
}

async function load() {
  loading.value = true
  try {
    const d = await api.get('/api/categories')
    groups.value = d.groups || {}
    domains.value = d.domains || []
    upRules.value = d.up_rules || []
  } catch (e) {
    flash('err', e instanceof ApiError ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

async function addCategory() {
  if (!form.value.name.trim()) return flash('err', '分类名不能为空')
  busy.value = true
  try {
    const c = await categoryApi.add(form.value.domain.trim() || 'general',
                                    form.value.name.trim())
    flash('ok', `已添加 ${c.domain} / ${c.name}`)
    form.value = { domain: form.value.domain, name: '' }
    await load()
  } catch (e) {
    flash('err', e.message)
  } finally {
    busy.value = false
  }
}

async function confirmCat(c) {
  busy.value = true
  try {
    await categoryApi.confirm(c.id)
    flash('ok', `已采纳「${c.name}」`)
    await load()
  } catch (e) {
    flash('err', e.message)
  } finally {
    busy.value = false
  }
}

async function removeCat(c) {
  const extra = c.item_count
    ? `该分类下 ${c.item_count} 个条目会退回待整理箱。` : ''
  if (!window.confirm(`删除分类「${c.name}」?\n${extra}该分类的专属提示词与 UP主规则也会一并删除。`)) return
  busy.value = true
  try {
    const d = await categoryApi.remove(c.id)
    flash('ok', `已删除「${d.name}」:条目退回 ${d.items} 条,清理提示词 ${d.prompts} 条、UP规则 ${d.up_rules} 条`)
    await load()
  } catch (e) {
    flash('err', e.message)
  } finally {
    busy.value = false
  }
}

async function unbind(rule) {
  busy.value = true
  try {
    await categoryApi.unbindUp(rule.up_mid)
    flash('ok', `已解除 ${rule.up_name || rule.up_mid} 的规则`)
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
    <h3 style="margin:4px 0 12px">分类树</h3>

    <div v-if="toast" class="card">
      <span :class="toast.kind === 'err' ? 'tag warn' : 'tag'">
        {{ toast.kind === 'err' ? '出错' : '完成' }}</span>
      <span class="muted" style="margin-left:8px">{{ toast.text }}</span>
    </div>

    <div class="card">
      <form style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"
            @submit.prevent="addCategory">
        <input v-model="form.domain" list="domain-options" placeholder="领域,如 fitness/shopping"
               style="padding:6px 10px;border:1px solid var(--input-border);border-radius:6px;width:200px">
        <datalist id="domain-options">
          <option v-for="d in domains" :key="d" :value="d"></option>
        </datalist>
        <input v-model="form.name" placeholder="新分类名称" required
               style="padding:6px 10px;border:1px solid var(--input-border);border-radius:6px;width:200px">
        <button :disabled="busy">添加分类</button>
        <span class="muted">LLM 自动分类只能从这里"选题";"待确认"的提议点确认后生效。</span>
      </form>
    </div>

    <div v-for="(cats, dom) in groups" :key="dom" class="card">
      <h3>
        <span class="tag">{{ dom }}</span>
        <span class="muted" style="font-weight:400">{{ cats.length }} 个分类</span>
      </h3>
      <div v-for="c in cats" :key="c.id"
           style="display:flex;align-items:center;gap:10px;padding:7px 4px;border-bottom:1px dashed var(--card-border)">
        <span style="min-width:140px">{{ c.name }}</span>
        <span class="tag gray">{{ c.item_count }} 条目</span>
        <template v-if="c.status === 'proposed'">
          <span class="tag warn">待确认(LLM 提议)</span>
          <button :disabled="busy" @click="confirmCat(c)">✔ 确认采纳</button>
        </template>
        <span v-else class="tag gray">使用中</span>
        <button class="ghost" :disabled="busy" @click="removeCat(c)">🗑 删除</button>
      </div>
    </div>

    <div v-if="!loading && !Object.keys(groups).length" class="card empty">
      <h3>还没有分类</h3>
      <p class="muted">先建几个分类(如 fitness 下的"胸/背/腿"),LLM 自动分类才有"选择题"可做。</p>
    </div>

    <div v-if="upRules.length" class="card">
      <h3>UP主 → 分类 规则
        <span class="muted" style="font-weight:400">这些 UP 的新视频直接归档,不再走 LLM 分类</span>
      </h3>
      <div v-for="r in upRules" :key="r.up_mid"
           style="display:flex;align-items:center;gap:10px;padding:7px 4px;border-bottom:1px dashed var(--card-border)">
        <span style="min-width:180px">{{ r.up_name || ('mid ' + r.up_mid) }}</span>
        <span class="tag gray">mid {{ r.up_mid }}</span>
        <span class="tag">{{ r.category_domain }} / {{ r.category_name }}</span>
        <button class="ghost" :disabled="busy" @click="unbind(r)">解除</button>
      </div>
      <p class="muted" style="margin:8px 0 0">
        绑定入口在视频条目的详情页(「📌 该 UP 的视频都归此分类」)。</p>
    </div>
  </div>
</template>
