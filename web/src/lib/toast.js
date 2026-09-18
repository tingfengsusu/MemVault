/**
 * 全局提示(悬浮在右下角,不随页面滚动跑掉 —— 页尾按钮点击后也一定看得见)。
 * 用法:const toast = useToast(); toast.show('ok', '已排入队列');
 */
import { reactive } from 'vue'

let seq = 0
export const toastState = reactive({ items: [] })

export function showToast(kind, text, ttl = 5000) {
  const id = ++seq
  toastState.items.push({ id, kind, text })
  setTimeout(() => dismissToast(id), ttl)
  return id
}

export function dismissToast(id) {
  const i = toastState.items.findIndex(t => t.id === id)
  if (i >= 0) toastState.items.splice(i, 1)
}

export function useToast() {
  return { show: showToast, dismiss: dismissToast, state: toastState }
}
