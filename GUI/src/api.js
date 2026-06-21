const API_ROOT = import.meta.env.VITE_API_ROOT || '/api/v1'

async function request(path, options = {}) {
  const response = await fetch(`${API_ROOT}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  })
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`
    try {
      const payload = await response.json()
      detail = typeof payload.detail === 'string'
        ? payload.detail
        : payload.detail?.message || detail
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
  sessions: () => request('/sessions'),
  session: (id) => request(`/sessions/${id}`),
  createSession: () => request('/sessions', { method: 'POST', body: JSON.stringify({}) }),
  deleteSession: (id) => request(`/sessions/${id}`, { method: 'DELETE' }),
  send: (id, message, thinkingEnabled = false) => request(`/sessions/${id}/messages`, {
    method: 'POST',
    body: JSON.stringify({ message, thinking_enabled: thinkingEnabled }),
  }),
}
