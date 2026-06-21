import { useEffect, useRef, useState } from 'react'
import {
  Bot, ChevronLeft, CirclePlus, Menu, MessageSquareText,
  Brain, PanelLeftClose, Send, Settings2, Sparkles, Trash2, UserRound,
} from 'lucide-react'
import { api } from './api.js'

function Welcome() {
  return (
    <div className="welcome">
      <div className="welcome-mark"><Sparkles size={25} /></div>
      <p className="eyebrow">LOCAL AI ASSISTANT</p>
      <h1>今天想一起完成什么？</h1>
      <p>这是你的本地智能工作台。先从一次简单对话开始，之后我们会逐步加入文件、知识库与任务能力。</p>
    </div>
  )
}

function Message({ message }) {
  const assistant = message.role === 'assistant'
  return (
    <article className={`message ${assistant ? 'assistant' : 'user'}`}>
      <div className="avatar">{assistant ? <Bot size={18} /> : <UserRound size={17} />}</div>
      <div className="message-body">
        <div className="message-meta">{assistant ? 'Agent Dogs' : '你'}</div>
        {assistant && message.reasoning && (
          <details className="reasoning-block">
            <summary><Brain size={14} />思考过程</summary>
            <div>{message.reasoning}</div>
          </details>
        )}
        <div className="message-content">{message.content}</div>
        {message.backend && <span className="backend-chip">{message.backend}</span>}
      </div>
    </article>
  )
}

export default function App() {
  const [sessions, setSessions] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [status, setStatus] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [sidebar, setSidebar] = useState(true)
  const [thinkingEnabled, setThinkingEnabled] = useState(false)
  const bottomRef = useRef(null)

  useEffect(() => {
    Promise.all([api.sessions(), api.status()])
      .then(async ([items, backendStatus]) => {
        setStatus(backendStatus)
        if (items.length) {
          setSessions(items)
          await openSession(items[0].id)
        } else {
          await createSession()
        }
      })
      .catch((err) => setError(`无法连接后端：${err.message}`))
  }, [])

  useEffect(() => bottomRef.current?.scrollIntoView({ behavior: 'smooth' }), [messages, busy])

  async function refreshSessions() {
    setSessions(await api.sessions())
  }

  async function createSession() {
    setError('')
    const created = await api.createSession()
    setSessions((current) => [created, ...current])
    setActiveId(created.id)
    setMessages([])
    if (window.innerWidth < 760) setSidebar(false)
  }

  async function openSession(id) {
    const detail = await api.session(id)
    setActiveId(id)
    setMessages(detail.messages)
    setError('')
    if (window.innerWidth < 760) setSidebar(false)
  }

  async function removeSession(event, id) {
    event.stopPropagation()
    await api.deleteSession(id)
    const remaining = sessions.filter((item) => item.id !== id)
    setSessions(remaining)
    if (activeId === id) {
      if (remaining.length) await openSession(remaining[0].id)
      else await createSession()
    }
  }

  async function submit(event) {
    event.preventDefault()
    const text = input.trim()
    if (!text || busy || !activeId) return
    setInput('')
    setError('')
    setMessages((current) => [...current, { role: 'user', content: text }])
    setBusy(true)
    try {
      const result = await api.send(activeId, text, thinkingEnabled)
      setMessages((current) => [...current, {
        ...result.message,
        backend: result.backend,
        reasoning: result.reasoning,
        thinkingEnabled: result.thinking_enabled,
      }])
      await refreshSessions()
    } catch (err) {
      setError(err.message)
      setMessages((current) => current.slice(0, -1))
      setInput(text)
    } finally {
      setBusy(false)
    }
  }

  function onKeyDown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      event.currentTarget.form?.requestSubmit()
    }
  }

  const enabled = status?.backends.filter((item) => item.enabled) || []
  return (
    <div className={`app-shell ${sidebar ? '' : 'sidebar-closed'}`}>
      <aside className="sidebar">
        <div className="brand-row">
          <div className="brand"><span className="brand-mark"><Bot size={20} /></span><span>Agent Dogs</span></div>
          <button className="icon-button desktop-only" onClick={() => setSidebar(false)} aria-label="收起侧栏"><PanelLeftClose size={19} /></button>
        </div>
        <button className="new-chat" onClick={createSession}><CirclePlus size={18} />新建会话</button>
        <div className="section-label">最近会话</div>
        <nav className="session-list">
          {sessions.map((session) => (
            <button key={session.id} className={`session-item ${activeId === session.id ? 'active' : ''}`} onClick={() => openSession(session.id)}>
              <MessageSquareText size={17} />
              <span>{session.title}</span>
              <Trash2 className="delete-session" size={15} onClick={(event) => removeSession(event, session.id)} />
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className="model-status">
            <span className={`status-dot ${enabled.length ? 'online' : ''}`} />
            <div><strong>{enabled.length ? '模型已就绪' : '模型未配置'}</strong><small>{enabled.map((item) => item.name).join(' · ') || '请检查设置'}</small></div>
          </div>
          <button className="settings-button"><Settings2 size={18} />设置<span>即将开放</span></button>
        </div>
      </aside>

      {sidebar && <button className="mobile-scrim" onClick={() => setSidebar(false)} aria-label="关闭侧栏" />}
      <main className="chat-panel">
        <header className="topbar">
          <button className="icon-button" onClick={() => setSidebar(true)} aria-label="打开侧栏">{sidebar ? <ChevronLeft size={20} /> : <Menu size={20} />}</button>
          <div><strong>{sessions.find((item) => item.id === activeId)?.title || '新会话'}</strong><span>本地会话</span></div>
        </header>
        <section className="conversation">
          <div className="conversation-inner">
            {!messages.length ? <Welcome /> : messages.map((message, index) => <Message key={`${index}-${message.role}`} message={message} />)}
            {busy && <div className="thinking"><span /><span /><span /> Agent 正在思考</div>}
            <div ref={bottomRef} />
          </div>
        </section>
        <div className="composer-wrap">
          {error && <div className="error-banner">{error}</div>}
          <div className="composer-options">
            <label className={`thinking-toggle ${thinkingEnabled ? 'active' : ''}`}>
              <input
                type="checkbox"
                checked={thinkingEnabled}
                onChange={(event) => setThinkingEnabled(event.target.checked)}
                disabled={busy}
              />
              <Brain size={15} />
              深度思考
            </label>
          </div>
          <form className="composer" onSubmit={submit}>
            <textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={onKeyDown} placeholder="给 Agent Dogs 发送消息…" rows="1" disabled={busy} />
            <button type="submit" disabled={!input.trim() || busy} aria-label="发送"><Send size={19} /></button>
          </form>
          <p className="composer-note">Enter 发送 · Shift + Enter 换行 · 模型输出可能存在错误，请核对重要信息</p>
        </div>
      </main>
    </div>
  )
}
