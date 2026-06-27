const API_ROOT = import.meta.env.VITE_API_ROOT || '/api/v1'

function fileUrl(path, route = 'raw') {
  const query = new URLSearchParams({ path: path || '' })
  return `${API_ROOT}/files/${route}?${query.toString()}`
}

async function request(path, options = {}) {
  const bodyIsForm = options.body instanceof FormData
  const { headers: optionHeaders, ...fetchOptions } = options
  const headers = bodyIsForm
    ? { ...optionHeaders }
    : { 'Content-Type': 'application/json', ...optionHeaders }
  const response = await fetch(`${API_ROOT}${path}`, {
    ...fetchOptions,
    headers,
  })
  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    try {
      const payload = await response.json()
      if (typeof payload.detail === 'string') {
        detail = payload.detail
      } else if (payload.detail?.message && payload.detail?.reason) {
        detail = `${payload.detail.message}: ${payload.detail.reason}`
      } else {
        detail = payload.detail?.message || payload.detail?.reason || detail
      }
    } catch {
      // Keep the HTTP fallback message.
    }
    throw new Error(detail)
  }
  return response.status === 204 ? null : response.json()
}

export const api = {
  status: () => request('/status'),
  models: (provider) => request(`/models${provider ? `?provider=${encodeURIComponent(provider)}` : ''}`),
  tools: () => request('/tools'),
  agents: () => request('/agents'),
  sessions: () => request('/sessions'),
  session: (id) => request(`/sessions/${id}`),
  createSession: () => request('/sessions', { method: 'POST', body: JSON.stringify({}) }),
  deleteSession: (id) => request(`/sessions/${id}`, { method: 'DELETE' }),
  fileTree: () => request('/files/tree'),
  fileContent: (path) => request(`/files/content?${new URLSearchParams({ path }).toString()}`),
  saveFileContent: (path, content) => request('/files/content', {
    method: 'PUT',
    body: JSON.stringify({ path, content }),
  }),
  createFileItem: (path, name, type) => request('/files', {
    method: 'POST',
    body: JSON.stringify({ path, name, type }),
  }),
  renameFileItem: (path, name) => request('/files', {
    method: 'PATCH',
    body: JSON.stringify({ path, name }),
  }),
  deleteFileItem: (path) => request(`/files?${new URLSearchParams({ path }).toString()}`, { method: 'DELETE' }),
  uploadFile: (path, file) => {
    const form = new FormData()
    form.append('file', file)
    return request(`/files/upload?${new URLSearchParams({ path: path || '' }).toString()}`, {
      method: 'POST',
      headers: {},
      body: form,
    })
  },
  rawFileUrl: (path) => fileUrl(path, 'raw'),
  downloadFileUrl: (path) => fileUrl(path, 'download'),
  send: (id, message, options = {}) => {
    const payload = {
      message,
      thinking_enabled: Boolean(options.thinkingEnabled),
    }
    if (options.provider) payload.provider = options.provider
    if (options.model) payload.model = options.model
    if (options.temperature !== '' && options.temperature !== null && options.temperature !== undefined) {
      payload.temperature = Number(options.temperature)
    }
    if (options.maxTokens !== '' && options.maxTokens !== null && options.maxTokens !== undefined) {
      payload.max_tokens = Number(options.maxTokens)
    }
    return request(`/sessions/${id}/messages`, {
      method: 'POST',
      body: JSON.stringify(payload),
      signal: options.signal,
    })
  },
  resume: (id, payload, options = {}) => request(`/sessions/${id}/resume`, {
    method: 'POST',
    body: JSON.stringify(payload),
    signal: options.signal,
  }),
  cancelSessionRun: (id) => request(`/sessions/${id}/cancel`, { method: 'POST' }),
}
