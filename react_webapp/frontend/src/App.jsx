import { useEffect, useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from './api'

const POLL_MS = 1000
const MAX_RENDER_MESSAGES = 180

function formatWhen(isoText) {
  if (!isoText) return '-'
  const date = new Date(isoText)
  if (Number.isNaN(date.getTime())) return String(isoText)
  const now = new Date()
  const sameDay = date.toDateString() === now.toDateString()
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  const isYesterday = date.toDateString() === yesterday.toDateString()
  const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (sameDay) return `Today - ${time}`
  if (isYesterday) return `Yesterday - ${time}`
  return `${date.getDate()}. ${date.getMonth() + 1}. ${date.getFullYear()} - ${time}`
}

function formatScheduledStatus(message) {
  const status = String(message?.scheduled_status || '').toLowerCase()
  const when = formatWhen(message?.scheduled_created_at)
  if (status === 'retry_scheduled') return `Failed, retry planned | ${when}`
  if (status === 'failed_no_retry') return `Failed, no retry | ${when}`
  return `Completed | ${when}`
}

function groupTasks(tasks) {
  const today = new Date()
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate())

  const groups = { today: [], yesterday: [], future: [] }
  for (const task of tasks || []) {
    const run = task?.next_run_utc ? new Date(task.next_run_utc) : null
    if (!run || Number.isNaN(run.getTime())) {
      groups.future.push(task)
      continue
    }
    if (run.toDateString() === today.toDateString()) {
      groups.today.push(task)
    } else if (run < startOfToday) {
      groups.yesterday.push(task)
    } else {
      groups.future.push(task)
    }
  }

  const byNextRunAsc = (a, b) => {
    const left = Date.parse(String(a?.next_run_utc || ''))
    const right = Date.parse(String(b?.next_run_utc || ''))
    if (Number.isNaN(left) && Number.isNaN(right)) return 0
    if (Number.isNaN(left)) return 1
    if (Number.isNaN(right)) return -1
    return left - right
  }
  groups.today.sort(byNextRunAsc)
  groups.yesterday.sort(byNextRunAsc)
  groups.future.sort(byNextRunAsc)
  return groups
}

function MarkdownText({ content }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer noopener" />,
        }}
      >
        {String(content || '')}
      </ReactMarkdown>
    </div>
  )
}

function TaskCard({ task, onPauseResume, onRunNow, onDelete, onSavePrompt }) {
  const [expanded, setExpanded] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draftPrompt, setDraftPrompt] = useState(String(task?.task_prompt || ''))

  useEffect(() => {
    if (!editing) {
      setDraftPrompt(String(task?.task_prompt || ''))
    }
  }, [task?.task_prompt, editing])

  const enabled = Boolean(task?.enabled)
  const status = enabled ? 'Active' : 'Paused'
  const nextRun = formatWhen(task?.next_run_utc)

  return (
    <article className={`task-card ${enabled ? 'task-active' : 'task-paused'}`}>
      <div className="task-title-row">
        <h4>{task?.title || task?.id || 'Untitled task'}</h4>
        <span className="task-badge">{status}</span>
      </div>
      <p className="task-meta"><strong>Schedule:</strong> {task?.schedule_text || '-'}</p>
      <p className="task-meta"><strong>Next run:</strong> {nextRun}</p>

      <div className="task-actions">
        <button className="btn ghost" onClick={() => onPauseResume(task)}>{enabled ? 'Pause' : 'Resume'}</button>
        <button className="btn ghost" onClick={() => onRunNow(task)}>Run now</button>
        <button className="btn danger" onClick={() => onDelete(task)}>Delete</button>
      </div>

      <button className="task-toggle" onClick={() => setExpanded((v) => !v)}>
        {expanded ? 'Hide details' : 'Show details'}
      </button>

      {expanded && (
        <div className="task-details">
          {editing ? (
            <>
              <textarea value={draftPrompt} onChange={(e) => setDraftPrompt(e.target.value)} rows={5} />
              <div className="task-actions">
                <button className="btn primary" onClick={() => onSavePrompt(task, draftPrompt).then(() => setEditing(false))}>Save text</button>
                <button className="btn ghost" onClick={() => { setEditing(false); setDraftPrompt(String(task?.task_prompt || '')) }}>Cancel</button>
              </div>
            </>
          ) : (
            <>
              <pre>{task?.task_prompt || ''}</pre>
              <button className="btn ghost" onClick={() => setEditing(true)}>Edit text</button>
            </>
          )}
        </div>
      )}
    </article>
  )
}

function MessageCard({
  message,
  index,
  scheduledOpen,
  setScheduledOpen,
  toolOpen,
  setToolOpen,
  onMarkRead,
}) {
  const isScheduled = String(message?.message_type || '').toLowerCase() === 'scheduled'
  const keyId = message?.scheduled_event_id || `msg-${index}`
  const isOpen = Boolean(scheduledOpen[keyId])
  const isUnread = isScheduled && !Boolean(message?.scheduled_read)
  const toolCalls = Array.isArray(message?.tool_calls) ? message.tool_calls : []
  const toolsKey = `tools-${keyId}`
  const toolsOpen = Boolean(toolOpen[toolsKey])
  const isUser = String(message?.role || '').toLowerCase() === 'user'

  const roleClass = isUser ? 'msg-user' : 'msg-assistant'
  const avatarSrc = isUser ? '/avatar-user.svg' : '/avatar-ai.svg'
  const avatarAlt = isUser ? 'User avatar' : 'Assistant avatar'
  const avatarClass = isUser ? 'avatar-user' : 'avatar-assistant'

  if (!isScheduled) {
    const isAssistant = !isUser
    return (
      <div className={`message-row message-row-conversation ${isUser ? 'message-row-user' : 'message-row-assistant'}`}>
        <div className={`message-avatar ${avatarClass}`}>
          <img src={avatarSrc} alt={avatarAlt} />
        </div>
        <article className={`message-card ${roleClass}`}>
          {isAssistant ? (
            <MarkdownText content={message?.content} />
          ) : (
            <div className="message-body">{message?.content || ''}</div>
          )}
          {toolCalls.length > 0 && (
            <div className="tools-wrap">
              <button className="tool-toggle" onClick={() => setToolOpen((prev) => ({ ...prev, [toolsKey]: !toolsOpen }))}>
                {toolsOpen ? 'Hide tool calls' : `Show tool calls (${toolCalls.length})`}
              </button>
              {toolsOpen && (
                <div className="tool-list">
                  {toolCalls.map((call, idx) => (
                    <div className="tool-card" key={`${toolsKey}-${idx}`}>
                      <div className="tool-name">{idx + 1}. {String(call?.name || 'unknown')}</div>
                      <pre>{JSON.stringify(call?.args ?? {}, null, 2)}</pre>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </article>
      </div>
    )
  }

  const statusText = formatScheduledStatus(message)
  const title = message?.task_title || message?.task_id || `Scheduled task #${index + 1}`

  return (
    <div className="message-row message-row-system">
      <article
        className={`message-card scheduled ${isUnread ? 'scheduled-unread' : ''} ${isUnread && !isOpen ? 'scheduled-unread-collapsed' : ''}`}
      >
        <button
          className="scheduled-header"
          onClick={async () => {
            const next = !isOpen
            setScheduledOpen((prev) => ({ ...prev, [keyId]: next }))
            if (next && isUnread && message?.scheduled_event_id) {
              await onMarkRead(message.scheduled_event_id)
            }
          }}
        >
          <span className="scheduled-arrow">{isOpen ? '▼' : '▶'}</span>
          <span className="scheduled-title">{title}</span>
          <span className="scheduled-meta">{isUnread ? 'Unread | ' : ''}{statusText}</span>
        </button>
        {isOpen && (
          <div className="scheduled-body">
            <MarkdownText content={message?.content} />
            {toolCalls.length > 0 && (
              <div className="tools-wrap">
                <button className="tool-toggle" onClick={() => setToolOpen((prev) => ({ ...prev, [toolsKey]: !toolsOpen }))}>
                  {toolsOpen ? 'Hide tool calls' : `Show tool calls (${toolCalls.length})`}
                </button>
                {toolsOpen && (
                  <div className="tool-list">
                    {toolCalls.map((call, idx) => (
                      <div className="tool-card" key={`${toolsKey}-${idx}`}>
                        <div className="tool-name">{idx + 1}. {String(call?.name || 'unknown')}</div>
                        <pre>{JSON.stringify(call?.args ?? {}, null, 2)}</pre>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </article>
    </div>
  )
}

export default function App() {
  const [messages, setMessages] = useState([])
  const [tasks, setTasks] = useState([])
  const [lastEventId, setLastEventId] = useState(0)
  const [prompt, setPrompt] = useState('')
  const [sending, setSending] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')
  const [streamingReply, setStreamingReply] = useState('')
  const [manualRuns, setManualRuns] = useState({})
  const [scheduledOpen, setScheduledOpen] = useState({})
  const [toolOpen, setToolOpen] = useState({})
  const [taskGroupOpen, setTaskGroupOpen] = useState({
    today: true,
    yesterday: false,
    future: false,
  })

  useEffect(() => {
    let mounted = true
    ;(async () => {
      try {
        const data = await api.bootstrap()
        if (!mounted) return
        setMessages(Array.isArray(data?.messages) ? data.messages : [])
        setTasks(Array.isArray(data?.tasks) ? data.tasks : [])
        setLastEventId(Number(data?.processed_event_id || 0))
      } catch (err) {
        if (!mounted) return
        setError(`Bootstrap failed: ${String(err)}`)
      } finally {
        if (mounted) setLoading(false)
      }
    })()

    return () => {
      mounted = false
    }
  }, [])

  useEffect(() => {
    if (loading) return undefined

    const timer = setInterval(async () => {
      try {
        const data = await api.poll(lastEventId)
        const newMessages = Array.isArray(data?.new_messages) ? data.new_messages : []
        if (newMessages.length > 0) {
          setMessages((prev) => [...prev, ...newMessages])
        }
        const completedTaskIds = new Set(
          newMessages
            .map((item) => String(item?.task_id || '').trim())
            .filter((value) => value.length > 0),
        )
        setManualRuns((prev) => {
          const nowMs = Date.now()
          const next = {}
          for (const [taskId, payload] of Object.entries(prev)) {
            const startedAtMs = Number(payload?.startedAtMs || 0)
            const isCompleted = completedTaskIds.has(String(taskId))
            const isStale = startedAtMs > 0 && nowMs - startedAtMs > 30 * 60 * 1000
            if (!isCompleted && !isStale) {
              next[taskId] = payload
            }
          }
          return next
        })
        setTasks(Array.isArray(data?.tasks) ? data.tasks : [])
        setLastEventId(Number(data?.last_event_id || lastEventId))
      } catch {
        // Keep polling resilient; UI remains usable.
      }
    }, POLL_MS)

    return () => clearInterval(timer)
  }, [lastEventId, loading])

  const groupedTasks = useMemo(() => groupTasks(tasks), [tasks])
  const taskSections = [
    { key: 'today', label: 'Today' },
    { key: 'yesterday', label: 'Yesterday' },
    { key: 'future', label: 'Future' },
  ]

  async function sendPrompt(e) {
    e.preventDefault()
    const text = prompt.trim()
    if (!text || sending) return

    setSending(true)
    setError('')
    setInfo('')
    setStreamingReply('')

    const optimistic = { role: 'user', content: text }
    setMessages((prev) => [...prev, optimistic])
    setPrompt('')

    try {
      let sawPartial = false
      const data = await api.sendChatStream(text, {
        onPartial: (partialText) => {
          sawPartial = true
          setStreamingReply(String(partialText || ''))
        },
      })

      const assistant = data?.assistant_message
      if (assistant) {
        setMessages((prev) => [...prev, assistant])
      }
      if (Array.isArray(data?.tasks)) {
        setTasks(data.tasks)
      }
      if (!assistant && !sawPartial) {
        throw new Error('No assistant response received from streaming endpoint.')
      }
    } catch (err) {
      setError(`Send failed: ${String(err)}`)
    } finally {
      setStreamingReply('')
      setSending(false)
    }
  }

  async function clearChat() {
    try {
      await api.clearChat()
      setMessages([])
      setInfo('Chat cleared.')
    } catch (err) {
      setError(`Clear failed: ${String(err)}`)
    }
  }

  async function onPauseResume(task) {
    try {
      const updated = await api.patchTask(task.id, { enabled: !task.enabled })
      setTasks((prev) => prev.map((item) => (item.id === updated.id ? updated : item)))
    } catch (err) {
      setError(`Task update failed: ${String(err)}`)
    }
  }

  async function onRunNow(task) {
    try {
      const result = await api.runTaskNow(task.id)
      if (Boolean(result?.started)) {
        const title = String(result?.task_title || task?.title || task?.id || 'Task')
        setManualRuns((prev) => ({
          ...prev,
          [String(task.id)]: {
            taskId: String(task.id),
            title,
            startedAtMs: Date.now(),
          },
        }))
        setInfo('')
      } else {
        setInfo(result?.message || `Manual run could not be started for ${task.id}.`)
      }
    } catch (err) {
      setError(`Run now failed: ${String(err)}`)
    }
  }

  async function onDelete(task) {
    try {
      const result = await api.deleteTask(task.id)
      if (result?.removed) {
        setTasks((prev) => prev.filter((item) => item.id !== task.id))
      }
    } catch (err) {
      setError(`Delete failed: ${String(err)}`)
    }
  }

  async function onSavePrompt(task, nextPrompt) {
    try {
      const updated = await api.patchTask(task.id, { task_prompt: nextPrompt })
      setTasks((prev) => prev.map((item) => (item.id === updated.id ? updated : item)))
      setInfo(`Task ${task.id} text updated.`)
    } catch (err) {
      setError(`Save text failed: ${String(err)}`)
      throw err
    }
  }

  async function onMarkRead(scheduledEventId) {
    try {
      await api.markScheduledRead(scheduledEventId)
      setMessages((prev) =>
        prev.map((msg) =>
          String(msg?.scheduled_event_id || '') === String(scheduledEventId)
            ? { ...msg, scheduled_read: true }
            : msg,
        ),
      )
    } catch {
      // Keep UI responsive even when this write fails.
    }
  }

  const renderedMessages = messages.slice(-MAX_RENDER_MESSAGES)

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-header">
          <h1>Schedule Assistant</h1>
          <p>Task Operations</p>
        </div>

        <section className="task-group">
          {taskSections.map((section) => {
            const sectionTasks = groupedTasks[section.key] || []
            const isOpen = Boolean(taskGroupOpen[section.key])
            return (
              <div className="task-group-block" key={section.key}>
                <button
                  className="task-group-toggle"
                  onClick={() =>
                    setTaskGroupOpen((prev) => ({ ...prev, [section.key]: !isOpen }))
                  }
                >
                  <span>{isOpen ? '▼' : '▶'}</span>
                  <span>{section.label} ({sectionTasks.length})</span>
                </button>

                {isOpen && (
                  <div className="task-group-content">
                    {sectionTasks.length === 0 ? (
                      <p className="task-group-empty">No tasks.</p>
                    ) : (
                      sectionTasks.map((task) => (
                        <TaskCard
                          key={task.id}
                          task={task}
                          onPauseResume={onPauseResume}
                          onRunNow={onRunNow}
                          onDelete={onDelete}
                          onSavePrompt={onSavePrompt}
                        />
                      ))
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </section>
      </aside>

      <main className="chat-main">
        <header className="chat-header">
          <div>
            <h2>Assistant Chat</h2>
            <p>Monitor scheduled outputs and manage tasks in one place.</p>
          </div>
          <div className="header-actions">
            <span className="api-pill">API: {api.baseUrl}</span>
            <button className="btn ghost" onClick={clearChat}>Clear chat</button>
          </div>
        </header>

        <div className="notice-stack">
          {error ? <div className="notice error">{error}</div> : null}
          {info ? <div className="notice info">{info}</div> : null}
        </div>

        <section className="messages">
          {loading ? <div className="notice info">Loading...</div> : null}
          {!loading && renderedMessages.length === 0 ? (
            <div className="empty-state">No messages yet. Start with a question or scheduling command.</div>
          ) : null}

          {renderedMessages.map((message, index) => (
            <MessageCard
              key={`${message?.scheduled_event_id || 'msg'}-${index}`}
              message={message}
              index={index}
              scheduledOpen={scheduledOpen}
              setScheduledOpen={setScheduledOpen}
              toolOpen={toolOpen}
              setToolOpen={setToolOpen}
              onMarkRead={onMarkRead}
            />
          ))}

          {sending ? (
            <div className="message-row message-row-conversation message-row-assistant" aria-live="polite">
              <div className="message-avatar avatar-assistant">
                <img src="/avatar-ai.svg" alt="Assistant avatar" />
              </div>
              <article className="message-card msg-assistant pending-response">
                {streamingReply ? (
                  <MarkdownText content={streamingReply} />
                ) : (
                  <div className="pending-response-title">Assistant is responding...</div>
                )}
                <div className="typing-dots" aria-hidden="true">
                  <span />
                  <span />
                  <span />
                </div>
              </article>
            </div>
          ) : null}

          {Object.values(manualRuns).map((run) => (
            <article className="manual-run-card" key={`manual-${run.taskId}`}>
              <div className="manual-run-title">Running task "{run.title}" manually...</div>
              <div className="manual-run-subtitle">Waiting for completion result.</div>
              <div className="manual-run-progress" role="progressbar" aria-label={`Running ${run.title}`}>
                <span className="manual-run-progress-indicator" />
              </div>
            </article>
          ))}
        </section>

        <form className="composer" onSubmit={sendPrompt}>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                e.currentTarget.form?.requestSubmit()
              }
            }}
            placeholder="Message..."
            rows={2}
            disabled={sending}
          />
          <button className="btn primary" type="submit" disabled={sending || !prompt.trim()}>
            {sending ? 'Sending...' : 'Send'}
          </button>
        </form>
      </main>
    </div>
  )
}
