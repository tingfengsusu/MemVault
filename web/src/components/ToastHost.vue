<script setup>
/**
 * 悬浮提示容器:固定在右下角(点击页尾按钮也能立刻看到反馈)。
 * 每个页面挂一次即可;toast 内容来自 lib/toast.js 的全局状态。
 */
import { toastState } from '../lib/toast.js'

const style = {
  position: 'fixed', right: '18px', bottom: '18px', zIndex: 50,
  display: 'flex', flexDirection: 'column', gap: '8px',
  maxWidth: '360px', pointerEvents: 'none',
}
</script>

<template>
  <div :style="style">
    <div v-for="t in toastState.items" :key="t.id" class="card"
         :style="{ borderColor: t.kind === 'err' ? '#c0392b' : 'var(--card-border)',
                   boxShadow: '0 4px 16px rgba(0,0,0,.12)', margin: 0 }">
      <span :class="t.kind === 'err' ? 'tag warn' : 'tag ok'">
        {{ t.kind === 'err' ? '出错' : (t.kind === 'info' ? '提示' : '完成') }}</span>
      <span style="margin-left:8px;font-size:13px">{{ t.text }}</span>
    </div>
  </div>
</template>
