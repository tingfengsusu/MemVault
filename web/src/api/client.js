/**
 * 统一的接口调用层:一处实现契约解包与错误归一。
 * 后端契约:{ok:true, data, error:null} | {ok:false, data:null, error:{code,message}}
 */
export class ApiError extends Error {
  constructor(code, message, status) {
    super(message || code)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

async function request(method, path, { json, form, params } = {}) {
  const url = new URL(path, window.location.origin)
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v)
    }
  }
  const init = { method, headers: {} }
  if (json !== undefined) {
    init.headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(json)
  } else if (form !== undefined) {
    init.body = form
  }
  let resp
  try {
    resp = await fetch(url, init)
  } catch (e) {
    throw new ApiError('network_error', '连不上本地服务(托盘在运行吗?)', 0)
  }
  let body = null
  try {
    body = await resp.json()
  } catch {
    throw new ApiError('bad_response', `服务返回了非 JSON(${resp.status})`, resp.status)
  }
  if (!body || typeof body.ok !== 'boolean') {
    throw new ApiError('bad_contract', '响应不符合 {ok,data,error} 契约', resp.status)
  }
  if (!body.ok) {
    const err = body.error || {}
    throw new ApiError(err.code || `http_${resp.status}`, err.message, resp.status)
  }
  return body.data
}

export const api = {
  get: (path, params) => request('GET', path, { params }),
  post: (path, json) => request('POST', path, { json }),
  postForm: (path, form) => request('POST', path, { form }),
}

// ── 各页面的具体接口(集中在这里,组件不直接拼 URL)──────────────────
export const inboxApi = {
  load: ({ domain = '', page = 1, pageSize = 50 } = {}) =>
    api.get('/api/inbox', { domain, page, page_size: pageSize }),
  batch: (payload) => api.post('/api/inbox/batch', payload),
  classify: (itemId, categoryId) =>
    api.post(`/api/items/${itemId}/classify`, { category_id: categoryId }),
  setStatus: (itemId, status) =>
    api.post(`/api/items/${itemId}/status`, { status }),
  reanalyze: (itemId) => api.post(`/api/items/${itemId}/reanalyze`),
}

export const itemApi = {
  reanalyze: (itemId) => api.post(`/api/items/${itemId}/reanalyze`),
  cart: (itemId) => api.post(`/api/items/${itemId}/cart`),
  setStatus: (itemId, status) => api.post(`/api/items/${itemId}/status`, { status }),
  classify: (itemId, categoryId) =>
    api.post(`/api/items/${itemId}/classify`, { category_id: categoryId }),
  bindUp: (itemId, categoryId, upMid = '', upName = '') =>
    api.post(`/api/items/${itemId}/bind-up`,
             { category_id: Number(categoryId), up_mid: upMid || '', up_name: upName || '' }),
  unbindUp: (upMid) => api.post(`/api/up/${upMid}/unbind`, {}),
}

export const searchApi = {
  search: ({ q, domain = '', page = 1, pageSize = 12 } = {}) =>
    api.get('/api/items', { q, domain, page, page_size: pageSize }),
  images: ({ q, domain = '' } = {}) => api.get('/api/search/images', { q, domain }),
  /** 用库里已有的一帧找相似画面(条目详情页的「🔍 找相似画面」) */
  similar: (itemId, chunkId) =>
    api.post(`/api/items/${itemId}/similar-image`, { chunk_id: chunkId }),
  /** 以图搜图:multipart 上传,返回 {items, query_image, query_label} */
  uploadImage: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return requestForm('/api/search/image', fd)
  },
}

/** multipart 上传也走同一套契约解包 */
async function requestForm(path, formData) {
  let resp
  try {
    resp = await fetch(path, { method: 'POST', body: formData })
  } catch {
    throw new ApiError('network_error', '连不上本地服务(托盘在运行吗?)', 0)
  }
  let body
  try {
    body = await resp.json()
  } catch {
    throw new ApiError('bad_response', `服务返回了非 JSON(${resp.status})`, resp.status)
  }
  if (!body.ok) {
    const err = body.error || {}
    throw new ApiError(err.code || `http_${resp.status}`, err.message, resp.status)
  }
  return body.data
}
