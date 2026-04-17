const API_BASE = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
    ...options,
  })

  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || `HTTP ${response.status}`)
  }

  if (response.status === 204) {
    return null
  }
  return response.json()
}

function parseSseEvent(rawFrame) {
  const lines = String(rawFrame || '').split('\n')
  const dataLines = []
  for (const line of lines) {
    if (!line.startsWith('data:')) continue
    dataLines.push(line.slice(5).trimStart())
  }
  if (dataLines.length === 0) return null
  const payloadText = dataLines.join('\n').trim()
  if (!payloadText) return null
  return JSON.parse(payloadText)
}

async function streamChat(prompt, { onPartial, onDone, onError } = {}) {
  const response = await fetch(`${API_BASE}/api/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt }),
  })

  if (!response.ok) {
    const text = await response.text()
    throw new Error(text || `HTTP ${response.status}`)
  }
  if (!response.body) {
    throw new Error('Streaming response body is missing.')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let donePayload = null

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const frames = buffer.split('\n\n')
    buffer = frames.pop() || ''

    for (const frame of frames) {
      const event = parseSseEvent(frame)
      if (!event || typeof event !== 'object') continue
      const eventType = String(event.type || '')

      if (eventType === 'partial') {
        if (typeof onPartial === 'function') onPartial(String(event.text || ''))
      } else if (eventType === 'done') {
        donePayload = event
        if (typeof onDone === 'function') onDone(event)
      } else if (eventType === 'error') {
        if (typeof onError === 'function') onError(String(event.error || 'Unknown stream error'))
      }
    }
  }

  const trailing = parseSseEvent(buffer)
  if (trailing && typeof trailing === 'object') {
    const trailingType = String(trailing.type || '')
    if (trailingType === 'partial') {
      if (typeof onPartial === 'function') onPartial(String(trailing.text || ''))
    } else if (trailingType === 'done') {
      donePayload = trailing
      if (typeof onDone === 'function') onDone(trailing)
    } else if (trailingType === 'error') {
      if (typeof onError === 'function') onError(String(trailing.error || 'Unknown stream error'))
    }
  }

  return donePayload
}

export const api = {
  baseUrl: API_BASE,
  health: () => request('/api/health'),
  bootstrap: () => request('/api/bootstrap'),
  poll: (after) => request(`/api/poll?after=${encodeURIComponent(after)}`),
  sendChat: (prompt) => request('/api/chat', { method: 'POST', body: JSON.stringify({ prompt }) }),
  sendChatStream: (prompt, handlers) => streamChat(prompt, handlers),
  clearChat: () => request('/api/chat/clear', { method: 'POST' }),
  listTasks: () => request('/api/tasks'),
  patchTask: (taskId, payload) => request(`/api/tasks/${encodeURIComponent(taskId)}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteTask: (taskId) => request(`/api/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' }),
  runTaskNow: (taskId) => request(`/api/tasks/${encodeURIComponent(taskId)}/run-now`, { method: 'POST' }),
  markScheduledRead: (scheduledEventId) => request(`/api/messages/scheduled/${encodeURIComponent(scheduledEventId)}/read`, { method: 'POST' }),
}
