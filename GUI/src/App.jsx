import { useEffect, useRef, useState } from 'react'
import {
  AlertTriangle, Bot, Brain, ChevronDown, ChevronLeft, ChevronRight,
  CirclePlus, Download, Edit3, FilePlus, FileText, Folder, FolderOpen,
  FolderPlus, Image as ImageIcon, Menu, MessageSquareText, PanelLeftClose,
  RefreshCw, Save, Send, Settings2, Sparkles, StopCircle, Trash2, Upload, UserRound, X,
} from 'lucide-react'
import { api } from './api.js'

const PROVIDER_GROUPS = [
  { id: 'api', title: 'API 模型' },
  { id: 'ollama', title: 'Ollama' },
  { id: 'builtin', title: '内置模型' },
]

const DEBUG_PANEL_DEFAULT = String(import.meta.env.VITE_AGENT_DEBUG_PANEL || '').toLowerCase() === 'true'

function modelKey(model) {
  return model ? `${model.provider}:${model.model}` : ''
}

function pickDefaultModelKey(models, status) {
  if (!models.length) return ''
  const defaultKey = `${status?.default_provider || ''}:${status?.default_model || ''}`
  return models.some((item) => modelKey(item) === defaultKey)
    ? defaultKey
    : modelKey(models[0])
}

function parentPath(path) {
  const parts = (path || '').split('/').filter(Boolean)
  parts.pop()
  return parts.join('/')
}

function fileExtension(path = '') {
  const name = path.split('/').pop() || ''
  const index = name.lastIndexOf('.')
  return index >= 0 ? name.slice(index).toLowerCase() : ''
}

function isImageFile(file) {
  return file?.mime_type?.startsWith('image/')
}

function isPdfFile(file) {
  return file?.mime_type === 'application/pdf' || fileExtension(file?.path) === '.pdf'
}

function isHtmlFile(file) {
  return ['.html', '.htm'].includes(fileExtension(file?.path))
}

function isDocxFile(file) {
  return fileExtension(file?.path) === '.docx'
}

function formatBytes(bytes = 0) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function formatMessageTime(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function localMessage(role, content, extra = {}) {
  return {
    role,
    content,
    created_at: new Date().toISOString(),
    ...extra,
  }
}

function isAbortError(error) {
  return error?.name === 'AbortError'
}

function isAgentDebugPanelEnabled() {
  try {
    const value = localStorage.getItem('agentDebugPanel')
    if (value === 'true') return true
    if (value === 'false') return false
  } catch {
    // Ignore storage access errors and use the build-time default.
  }
  return DEBUG_PANEL_DEFAULT
}

function hasDebugValue(value) {
  if (Array.isArray(value)) return value.length > 0
  if (value && typeof value === 'object') return Object.keys(value).length > 0
  return value !== undefined && value !== null && value !== ''
}

function DebugJson({ value }) {
  if (!hasDebugValue(value)) return null
  return <pre>{JSON.stringify(value, null, 2)}</pre>
}

function DebugLine({ label, value }) {
  if (!hasDebugValue(value)) return null
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return (
    <div className="agent-debug-line">
      <span>{label}</span>
      <code>{text}</code>
    </div>
  )
}

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

function safeLinkHref(href) {
  const value = String(href || '').trim()
  return /^(https?:|mailto:)/i.test(value) ? value : ''
}

function renderInlineMarkdown(text, keyPrefix = 'inline') {
  const parts = []
  const source = String(text || '')
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\)|\*[^*]+\*)/g
  let lastIndex = 0
  let match
  while ((match = pattern.exec(source)) !== null) {
    if (match.index > lastIndex) parts.push(source.slice(lastIndex, match.index))
    const token = match[0]
    const key = `${keyPrefix}-${match.index}`
    if (token.startsWith('`')) {
      parts.push(<code key={key}>{token.slice(1, -1)}</code>)
    } else if (token.startsWith('**')) {
      parts.push(<strong key={key}>{renderInlineMarkdown(token.slice(2, -2), `${key}-strong`)}</strong>)
    } else if (token.startsWith('[')) {
      const link = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/)
      const href = safeLinkHref(link?.[2])
      parts.push(href ? (
        <a key={key} href={href} target="_blank" rel="noreferrer">
          {renderInlineMarkdown(link[1], `${key}-link`)}
        </a>
      ) : (
        <span key={key}>{link?.[1] || token}</span>
      ))
    } else if (token.startsWith('*')) {
      parts.push(<em key={key}>{renderInlineMarkdown(token.slice(1, -1), `${key}-em`)}</em>)
    }
    lastIndex = pattern.lastIndex
  }
  if (lastIndex < source.length) parts.push(source.slice(lastIndex))
  return parts
}

function parseMarkdownBlocks(content) {
  const lines = String(content || '').replace(/\r\n/g, '\n').split('\n')
  const blocks = []
  let paragraph = []
  let list = null
  let quote = []
  let code = null

  function flushParagraph() {
    if (!paragraph.length) return
    blocks.push({ type: 'paragraph', text: paragraph.join(' ') })
    paragraph = []
  }

  function flushList() {
    if (!list) return
    blocks.push(list)
    list = null
  }

  function flushQuote() {
    if (!quote.length) return
    blocks.push({ type: 'quote', lines: quote })
    quote = []
  }

  function flushTextBlocks() {
    flushParagraph()
    flushList()
    flushQuote()
  }

  lines.forEach((line) => {
    const fence = line.match(/^```(\S*)\s*$/)
    if (code) {
      if (fence) {
        blocks.push({ type: 'code', language: code.language, text: code.lines.join('\n') })
        code = null
      } else {
        code.lines.push(line)
      }
      return
    }
    if (fence) {
      flushTextBlocks()
      code = { language: fence[1] || '', lines: [] }
      return
    }
    if (!line.trim()) {
      flushTextBlocks()
      return
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/)
    if (heading) {
      flushTextBlocks()
      blocks.push({ type: 'heading', level: heading[1].length, text: heading[2].trim() })
      return
    }

    const unordered = line.match(/^\s*[-*]\s+(.+)$/)
    if (unordered) {
      flushParagraph()
      flushQuote()
      if (!list || list.kind !== 'ul') {
        flushList()
        list = { type: 'list', kind: 'ul', items: [] }
      }
      list.items.push(unordered[1])
      return
    }

    const ordered = line.match(/^\s*\d+\.\s+(.+)$/)
    if (ordered) {
      flushParagraph()
      flushQuote()
      if (!list || list.kind !== 'ol') {
        flushList()
        list = { type: 'list', kind: 'ol', items: [] }
      }
      list.items.push(ordered[1])
      return
    }

    const quoted = line.match(/^\s*>\s?(.*)$/)
    if (quoted) {
      flushParagraph()
      flushList()
      quote.push(quoted[1])
      return
    }

    flushList()
    flushQuote()
    paragraph.push(line.trim())
  })

  if (code) blocks.push({ type: 'code', language: code.language, text: code.lines.join('\n') })
  flushTextBlocks()
  return blocks.length ? blocks : [{ type: 'paragraph', text: String(content || '') }]
}

function MarkdownContent({ content }) {
  try {
    const blocks = parseMarkdownBlocks(content)
    return (
      <div className="message-content markdown-content">
        {blocks.map((block, index) => {
          if (block.type === 'heading') {
            const Heading = `h${block.level}`
            return <Heading key={index}>{renderInlineMarkdown(block.text, `h-${index}`)}</Heading>
          }
          if (block.type === 'code') {
            return (
              <pre key={index} data-language={block.language || undefined}>
                <code>{block.text}</code>
              </pre>
            )
          }
          if (block.type === 'list') {
            const ListTag = block.kind
            return (
              <ListTag key={index}>
                {block.items.map((item, itemIndex) => (
                  <li key={itemIndex}>{renderInlineMarkdown(item, `li-${index}-${itemIndex}`)}</li>
                ))}
              </ListTag>
            )
          }
          if (block.type === 'quote') {
            return (
              <blockquote key={index}>
                {block.lines.map((line, lineIndex) => (
                  <p key={lineIndex}>{renderInlineMarkdown(line, `q-${index}-${lineIndex}`)}</p>
                ))}
              </blockquote>
            )
          }
          return <p key={index}>{renderInlineMarkdown(block.text, `p-${index}`)}</p>
        })}
      </div>
    )
  } catch {
    return <div className="message-content plain-text">{content}</div>
  }
}

function AgentFlowPanel({ flow }) {
  if (!hasDebugValue(flow)) return null
  const mainAgent = flow.mainAgent || {}
  const subAgents = Array.isArray(flow.subAgents) ? flow.subAgents : []
  const tools = Array.isArray(flow.tools) ? flow.tools : []
  const errors = Array.isArray(flow.errors) ? flow.errors : []
  return (
    <div className="agent-flow">
      <section className="agent-flow-section">
        <h4>主 Agent</h4>
        <DebugLine label="名称" value={mainAgent.name} />
        <DebugLine label="路由" value={mainAgent.route} />
        <DebugLine label="复杂度" value={mainAgent.complexity} />
        <DebugLine label="状态" value={mainAgent.status} />
        <DebugLine label="路由原因" value={mainAgent.routeReason} />
        <details className="agent-flow-details">
          <summary>详细输入与分析</summary>
          <DebugJson value={{
            input: mainAgent.input,
            analysis: mainAgent.analysis,
            taskBrief: mainAgent.taskBrief,
            plan: mainAgent.plan,
            events: mainAgent.events,
          }} />
        </details>
      </section>

      <section className="agent-flow-section">
        <h4>子 Agent</h4>
        {subAgents.length ? subAgents.map((agent, index) => (
          <details className="agent-flow-details" key={`${agent.name || 'agent'}-${index}`} open={index === 0}>
            <summary>
              <strong>{agent.name || agent.type || 'SubAgent'}</strong>
              <span>{agent.status || 'unknown'}</span>
            </summary>
            <DebugLine label="类型" value={agent.type} />
            <DebugLine label="说明" value={agent.description} />
            <DebugLine label="能力" value={agent.capabilities} />
            <DebugJson value={{
              input: agent.input,
              output: agent.output,
              error: agent.error,
              relatedToolCalls: agent.relatedToolCalls,
            }} />
          </details>
        )) : <p className="agent-flow-empty">本次没有调用子 Agent。</p>}
      </section>

      <section className="agent-flow-section">
        <h4>工具调用</h4>
        {tools.length ? tools.map((tool, index) => (
          <details className="agent-flow-details" key={`${tool.name || 'tool'}-${index}`}>
            <summary>
              <strong>{tool.name || 'Tool'}</strong>
              <span>{tool.status || (tool.ok ? 'completed' : 'failed')}</span>
            </summary>
            <DebugJson value={{
              input: tool.input,
              output: tool.output,
              ok: tool.ok,
              error: tool.error,
            }} />
          </details>
        )) : <p className="agent-flow-empty">本次没有调用工具。</p>}
      </section>

      <section className="agent-flow-section">
        <h4>最终输出</h4>
        <DebugJson value={flow.finalOutput} />
      </section>

      {errors.length ? (
        <section className="agent-flow-section danger">
          <h4>错误</h4>
          <DebugJson value={errors} />
        </section>
      ) : null}
    </div>
  )
}

function AgentDebugPanel({ message }) {
  const payload = {
    route: message.route,
    complexity: message.complexity,
    status: message.status,
    planStatus: message.planStatus,
    toolCalls: message.toolCalls,
    steps: message.steps,
    debugTrace: message.debugTrace,
    agentFlow: message.agentFlow,
    taskBrief: message.taskBrief,
  }
  const hasDebug = Object.values(payload).some(hasDebugValue)
  if (!hasDebug) return null
  return (
    <details className="agent-debug-block">
      <summary>调试信息</summary>
      <div className="agent-debug-body">
        {hasDebugValue(message.agentFlow) ? <AgentFlowPanel flow={message.agentFlow} /> : null}
        {!hasDebugValue(message.agentFlow) && hasDebugValue(message.debugTrace) ? (
          <section className="agent-flow-section">
            <h4>Trace</h4>
            <DebugJson value={message.debugTrace} />
          </section>
        ) : null}
        <details className="agent-flow-details raw-json">
          <summary>原始 JSON</summary>
          <DebugJson value={payload} />
        </details>
      </div>
    </details>
  )
}

function Message({ message, onClarify, onPlan, canResume, showDebugPanel }) {
  const assistant = message.role === 'assistant'
  const interrupt = message.interrupt
  const time = formatMessageTime(message.created_at)
  const showDebug = assistant && showDebugPanel
  return (
    <article className={`message ${assistant ? 'assistant' : 'user'}`}>
      <div className="avatar">{assistant ? <Bot size={18} /> : <UserRound size={17} />}</div>
      <div className="message-body">
        <div className="message-meta">
          <span>{assistant ? 'Agent Dogs' : '你'}</span>
          {time ? <time dateTime={message.created_at}>{time}</time> : null}
        </div>
        {assistant && message.reasoning && (
          <details className="reasoning-block">
            <summary><Brain size={14} />思考过程</summary>
            <div>{message.reasoning}</div>
          </details>
        )}
        {assistant ? (
          <MarkdownContent content={message.content} />
        ) : (
          <div className="message-content plain-text">{message.content}</div>
        )}
        {assistant && canResume && interrupt?.type === 'clarification' ? (
          <button type="button" className="clarify-open" onClick={() => onClarify(interrupt)}>
            补充信息
          </button>
        ) : null}
        {assistant && canResume && interrupt?.type === 'plan_confirmation' ? (
          <button type="button" className="clarify-open" onClick={() => onPlan(interrupt)}>
            查看计划
          </button>
        ) : null}
        {showDebug ? <AgentDebugPanel message={message} /> : null}
        {message.backend && <span className="backend-chip">{message.backend}</span>}
      </div>
    </article>
  )
}

function interruptClarification(interrupt) {
  return interrupt?.clarification || null
}

function emptyClarifyAnswers(interrupt) {
  const clarification = interruptClarification(interrupt)
  return Object.fromEntries((clarification?.questions || []).map((question) => [question.id, '']))
}

function formatClarifyAnswers(interrupt, answers) {
  const clarification = interruptClarification(interrupt)
  const lines = ['补充信息：']
  clarification.questions.forEach((question, index) => {
    lines.push(`${index + 1}. ${question.question}`)
    lines.push(`   ${(answers[question.id] || '').trim()}`)
  })
  return lines.join('\n')
}

function assistantFromResult(result) {
  return {
    ...result.message,
    backend: `${result.provider}/${result.model}`,
    reasoning: result.reasoning,
    thinkingEnabled: result.thinking_enabled,
    route: result.route,
    complexity: result.complexity,
    clarification: result.clarification,
    planSteps: result.plan_steps,
    status: result.status,
    interrupt: result.interrupt,
    planStatus: result.plan_status,
    task: result.task,
    steps: result.steps,
    toolCalls: result.tool_calls,
    debugTrace: result.debug_trace,
    agentFlow: result.agent_flow,
    taskBrief: result.task_brief,
  }
}

function normalizeMessage(message) {
  return {
    ...message,
    planSteps: message.planSteps ?? message.plan_steps,
    planStatus: message.planStatus ?? message.plan_status,
    toolCalls: message.toolCalls ?? message.tool_calls,
    debugTrace: message.debugTrace ?? message.debug_trace,
    agentFlow: message.agentFlow ?? message.agent_flow,
    taskBrief: message.taskBrief ?? message.task_brief,
  }
}

function ClarifyDialog({ interrupt, answers, onAnswer, onClose, onSubmit, disabled }) {
  const clarification = interruptClarification(interrupt)
  if (!clarification) return null
  const ready = clarification.questions.every((question) => (
    !question.required || Boolean((answers[question.id] || '').trim())
  ))

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="clarify-modal" role="dialog" aria-modal="true" aria-labelledby="clarify-title">
        <div className="clarify-header">
          <div>
            <strong id="clarify-title">补充任务信息</strong>
            <span>回答后会自动继续运行当前任务</span>
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="关闭补充信息弹窗"><X size={18} /></button>
        </div>
        <div className="clarify-original">
          <span>原始任务</span>
          <p>{clarification.original_message}</p>
        </div>
        <div className="clarify-questions">
          {clarification.questions.map((question, index) => {
            const value = answers[question.id] || ''
            const customValue = question.options?.includes(value) ? '' : value
            return (
              <fieldset key={question.id} className="clarify-question">
                <legend>{index + 1}. {question.question}{question.required ? <span>*</span> : null}</legend>
                {question.options?.length ? (
                  <div className="clarify-options">
                    {question.options.map((option) => (
                      <label key={option}>
                        <input
                          type="radio"
                          name={`clarify-${question.id}`}
                          checked={value === option}
                          onChange={() => onAnswer(question.id, option)}
                        />
                        {option}
                      </label>
                    ))}
                  </div>
                ) : null}
                {question.allow_custom ? (
                  <textarea
                    value={customValue}
                    onChange={(event) => onAnswer(question.id, event.target.value)}
                    placeholder={question.options?.length ? '自定义回答' : '请输入补充信息'}
                    rows={2}
                  />
                ) : null}
              </fieldset>
            )
          })}
        </div>
        <div className="clarify-actions">
          <button type="button" onClick={onClose}>稍后再说</button>
          <button type="button" className="primary" onClick={onSubmit} disabled={!ready || disabled}>
            提交并继续
          </button>
        </div>
      </section>
    </div>
  )
}

function PlanDialog({ interrupt, feedback, onFeedback, onClose, onApprove, onRevise, onCancel, disabled }) {
  const plan = interrupt?.plan
  if (!plan) return null
  const canRevise = Boolean(feedback.trim())

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="clarify-modal plan-modal" role="dialog" aria-modal="true" aria-labelledby="plan-title">
        <div className="clarify-header">
          <div>
            <strong id="plan-title">确认执行计划</strong>
            <span>第一阶段只确认计划，不会自动执行复杂任务</span>
          </div>
          <button type="button" className="icon-button" onClick={onClose} aria-label="关闭计划确认弹窗"><X size={18} /></button>
        </div>
        <div className="plan-body">
          <section className="plan-section">
            <span>任务摘要</span>
            <p>{plan.summary || '等待确认的复杂任务计划。'}</p>
          </section>
          <section className="plan-section">
            <span>计划步骤</span>
            <ol>
              {(plan.steps || []).map((step, index) => <li key={`${index}-${step}`}>{step}</li>)}
            </ol>
          </section>
          {plan.risks?.length ? (
            <section className="plan-section">
              <span>风险与确认点</span>
              <ul>
                {plan.risks.map((risk, index) => <li key={`${index}-${risk}`}>{risk}</li>)}
              </ul>
            </section>
          ) : null}
          <label className="plan-feedback">
            <span>修改意见</span>
            <textarea
              value={feedback}
              onChange={(event) => onFeedback(event.target.value)}
              placeholder="如果计划需要调整，请写下你的修改意见"
              rows={3}
            />
          </label>
        </div>
        <div className="clarify-actions plan-actions">
          <button type="button" onClick={onCancel} disabled={disabled}>取消任务</button>
          <button type="button" onClick={onRevise} disabled={!canRevise || disabled}>提交修改意见</button>
          <button type="button" className="primary" onClick={onApprove} disabled={disabled}>确认计划</button>
        </div>
      </section>
    </div>
  )
}

function FileTreeNode({ node, depth, selectedPath, expandedDirs, onToggle, onSelect }) {
  const directory = node.type === 'directory'
  const expanded = directory && expandedDirs.has(node.path)
  const selected = selectedPath === node.path
  const visibleChildren = directory && expanded && node.children?.length

  return (
    <div>
      <button
        type="button"
        className={`file-tree-item ${selected ? 'active' : ''}`}
        style={{ paddingLeft: 8 + depth * 14 }}
        onClick={() => {
          onSelect(node)
          if (directory) onToggle(node.path)
        }}
      >
        {directory ? (
          expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />
        ) : <span className="tree-spacer" />}
        {directory ? (
          expanded ? <FolderOpen size={16} /> : <Folder size={16} />
        ) : <FileText size={15} />}
        <span>{node.name}</span>
      </button>
      {visibleChildren ? node.children.map((child) => (
        <FileTreeNode
          key={child.path}
          node={child}
          depth={depth + 1}
          selectedPath={selectedPath}
          expandedDirs={expandedDirs}
          onToggle={onToggle}
          onSelect={onSelect}
        />
      )) : null}
    </div>
  )
}

export default function App() {
  const [view, setView] = useState('chat')
  const [sessions, setSessions] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [status, setStatus] = useState(null)
  const [models, setModels] = useState([])
  const [selectedModelKey, setSelectedModelKey] = useState('')
  const [temperature, setTemperature] = useState('0.7')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [sidebar, setSidebar] = useState(true)
  const [thinkingEnabled, setThinkingEnabled] = useState(false)
  const [debugPanelEnabled, setDebugPanelEnabled] = useState(() => isAgentDebugPanelEnabled())
  const [clarifyDialog, setClarifyDialog] = useState(null)
  const [clarifyAnswers, setClarifyAnswers] = useState({})
  const [planDialog, setPlanDialog] = useState(null)
  const [planFeedback, setPlanFeedback] = useState('')
  const [fileTree, setFileTree] = useState(null)
  const [expandedDirs, setExpandedDirs] = useState(new Set(['']))
  const [selectedFile, setSelectedFile] = useState(null)
  const [fileContent, setFileContent] = useState(null)
  const [draftContent, setDraftContent] = useState('')
  const [fileError, setFileError] = useState('')
  const [fileBusy, setFileBusy] = useState(false)
  const bottomRef = useRef(null)
  const uploadInputRef = useRef(null)
  const activeRequestRef = useRef(null)

  useEffect(() => {
    Promise.all([api.sessions(), api.status(), api.models()])
      .then(async ([items, backendStatus, availableModels]) => {
        setStatus(backendStatus)
        setModels(availableModels)
        setSelectedModelKey((current) => current || pickDefaultModelKey(availableModels, backendStatus))
        if (items.length) {
          setSessions(items)
          await openSession(items[0].id)
        } else {
          await createSession()
        }
      })
      .catch((err) => setError(`无法连接后端：${err.message}`))
  }, [])

  useEffect(() => () => {
    activeRequestRef.current?.abort()
  }, [])

  useEffect(() => bottomRef.current?.scrollIntoView({ behavior: 'smooth' }), [messages, busy])

  useEffect(() => {
    if (view === 'files' && !fileTree) {
      refreshFiles()
    }
  }, [view, fileTree])

  const selectedModel = models.find((item) => modelKey(item) === selectedModelKey) || null
  const hasUnsavedFile = Boolean(fileContent?.editable && draftContent !== fileContent.content)

  function toggleDebugPanel() {
    setDebugPanelEnabled((current) => {
      const next = !current
      try {
        localStorage.setItem('agentDebugPanel', next ? 'true' : 'false')
      } catch {
        // Keep the in-memory toggle even when localStorage is unavailable.
      }
      return next
    })
  }

  useEffect(() => {
    if (selectedModel && !selectedModel.supports_thinking && thinkingEnabled) {
      setThinkingEnabled(false)
    }
  }, [selectedModel, thinkingEnabled])

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
    setMessages((detail.messages || []).map(normalizeMessage))
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

  function openClarification(clarification) {
    setClarifyDialog(clarification)
    setClarifyAnswers(emptyClarifyAnswers(clarification))
  }

  function openPlan(interrupt) {
    setPlanDialog(interrupt)
    setPlanFeedback('')
  }

  function openAgentInterrupt(interrupt) {
    if (interrupt?.type === 'clarification') {
      openClarification(interrupt)
    } else if (interrupt?.type === 'plan_confirmation') {
      openPlan(interrupt)
    }
  }

  function appendAgentResult(result) {
    setMessages((current) => [...current, assistantFromResult(result)])
    if (result.status === 'interrupted' && result.interrupt) {
      openAgentInterrupt(result.interrupt)
    }
  }

  async function cancelCurrentRun() {
    if (!busy || !activeId) return
    const controller = activeRequestRef.current
    activeRequestRef.current = null
    controller?.abort()
    setBusy(false)
    setMessages((current) => [...current, localMessage('assistant', '本轮回复已中断。', { status: 'cancelled' })])
    try {
      await api.cancelSessionRun(activeId)
      await refreshSessions()
    } catch (err) {
      if (!isAbortError(err)) setError(err.message)
    }
  }

  async function sendChatMessage(text, { restoreOnFail = false } = {}) {
    if (!text || busy || !activeId) return
    const modelForRequest = selectedModel
    const thinkingForRequest = Boolean(modelForRequest?.supports_thinking && thinkingEnabled)
    const controller = new AbortController()
    activeRequestRef.current = controller
    setError('')
    setMessages((current) => [...current, localMessage('user', text)])
    setBusy(true)
    try {
      const result = await api.send(activeId, text, {
        provider: modelForRequest?.provider,
        model: modelForRequest?.model,
        temperature,
        thinkingEnabled: thinkingForRequest,
        signal: controller.signal,
      })
      appendAgentResult(result)
      await refreshSessions()
    } catch (err) {
      if (isAbortError(err)) return
      setError(err.message)
      setMessages((current) => current.slice(0, -1))
      if (restoreOnFail) setInput((current) => (current.length ? current : text))
    } finally {
      if (activeRequestRef.current === controller) {
        activeRequestRef.current = null
        setBusy(false)
      }
    }
  }

  async function submit(event) {
    event.preventDefault()
    const text = input.trim()
    if (!text || busy || !activeId) return
    setInput('')
    await sendChatMessage(text, { restoreOnFail: true })
  }

  async function submitClarification() {
    if (!clarifyDialog || busy) return
    const interrupt = clarifyDialog
    const userText = formatClarifyAnswers(interrupt, clarifyAnswers)
    setClarifyDialog(null)
    setClarifyAnswers({})
    await resumeAgent(
      {
        interrupt_id: interrupt.id,
        type: 'clarification',
        answers: clarifyAnswers,
      },
      userText,
    )
  }

  async function resumeAgent(payload, userText) {
    if (busy || !activeId) return
    const controller = new AbortController()
    activeRequestRef.current = controller
    setError('')
    setMessages((current) => [...current, localMessage('user', userText)])
    setBusy(true)
    try {
      const result = await api.resume(activeId, payload, { signal: controller.signal })
      appendAgentResult(result)
      await refreshSessions()
    } catch (err) {
      if (isAbortError(err)) return
      setError(err.message)
      setMessages((current) => current.slice(0, -1))
    } finally {
      if (activeRequestRef.current === controller) {
        activeRequestRef.current = null
        setBusy(false)
      }
    }
  }

  async function submitPlanDecision(decision) {
    if (!planDialog || busy) return
    const interrupt = planDialog
    const feedback = planFeedback.trim()
    const userText = decision === 'approve'
      ? '确认计划，继续后续流程。'
      : decision === 'cancel'
        ? '取消当前任务。'
        : `请根据以下意见修改计划：\n${feedback}`
    setPlanDialog(null)
    setPlanFeedback('')
    await resumeAgent(
      {
        interrupt_id: interrupt.id,
        type: 'plan_confirmation',
        decision,
        feedback,
      },
      userText,
    )
  }

  function onKeyDown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
      if (busy) return
      event.preventDefault()
      event.currentTarget.form?.requestSubmit()
    }
  }

  async function refreshFiles() {
    setFileBusy(true)
    setFileError('')
    try {
      setFileTree(await api.fileTree())
    } catch (err) {
      setFileError(err.message)
    } finally {
      setFileBusy(false)
    }
  }

  function confirmDiscard() {
    return !hasUnsavedFile || window.confirm('当前文件有未保存内容，确定要放弃修改吗？')
  }

  async function selectFileNode(node) {
    if (!confirmDiscard()) return
    setSelectedFile(node)
    setFileError('')
    setFileContent(null)
    setDraftContent('')
    if (node.type === 'directory') return
    if (!node.editable && !isDocxFile(node)) return
    setFileBusy(true)
    try {
      const content = await api.fileContent(node.path)
      setFileContent(content)
      setDraftContent(content.content)
    } catch (err) {
      setFileError(err.message)
    } finally {
      setFileBusy(false)
    }
  }

  function toggleDirectory(path) {
    setExpandedDirs((current) => {
      const next = new Set(current)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  function selectedDirectoryPath() {
    if (!selectedFile) return ''
    return selectedFile.type === 'directory' ? selectedFile.path : parentPath(selectedFile.path)
  }

  async function createFileItem(type) {
    const name = window.prompt(type === 'directory' ? '文件夹名称' : '文件名称')
    if (!name) return
    setFileBusy(true)
    setFileError('')
    try {
      const created = await api.createFileItem(selectedDirectoryPath(), name, type)
      await refreshFiles()
      if (created.type === 'directory') setExpandedDirs((current) => new Set([...current, created.path]))
      await selectFileNode(created)
    } catch (err) {
      setFileError(err.message)
    } finally {
      setFileBusy(false)
    }
  }

  async function renameSelectedFile() {
    if (!selectedFile?.path) return
    const name = window.prompt('重命名为', selectedFile.name)
    if (!name || name === selectedFile.name) return
    setFileBusy(true)
    setFileError('')
    try {
      const renamed = await api.renameFileItem(selectedFile.path, name)
      await refreshFiles()
      await selectFileNode(renamed)
    } catch (err) {
      setFileError(err.message)
    } finally {
      setFileBusy(false)
    }
  }

  async function deleteSelectedFile() {
    if (!selectedFile?.path) return
    if (!window.confirm(`删除 ${selectedFile.name}？文件会移入 workspace/.trash。`)) return
    setFileBusy(true)
    setFileError('')
    try {
      await api.deleteFileItem(selectedFile.path)
      setSelectedFile(null)
      setFileContent(null)
      setDraftContent('')
      await refreshFiles()
    } catch (err) {
      setFileError(err.message)
    } finally {
      setFileBusy(false)
    }
  }

  async function saveCurrentFile() {
    if (!fileContent?.editable || !selectedFile) return
    setFileBusy(true)
    setFileError('')
    try {
      const saved = await api.saveFileContent(selectedFile.path, draftContent)
      setFileContent(saved)
      setDraftContent(saved.content)
      await refreshFiles()
    } catch (err) {
      setFileError(err.message)
    } finally {
      setFileBusy(false)
    }
  }

  async function uploadFile(event) {
    const [file] = event.target.files || []
    event.target.value = ''
    if (!file) return
    setFileBusy(true)
    setFileError('')
    try {
      const uploaded = await api.uploadFile(selectedDirectoryPath(), file)
      await refreshFiles()
      await selectFileNode(uploaded)
    } catch (err) {
      setFileError(err.message)
    } finally {
      setFileBusy(false)
    }
  }

  const enabled = status?.backends.filter((item) => item.enabled) || []
  const groupedModels = PROVIDER_GROUPS.map((group) => ({
    ...group,
    models: models.filter((item) => item.provider === group.id),
  }))
  const latestPendingInterruptIndex = (() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index]
      if (message.role === 'assistant') {
        return message.status === 'interrupted' && message.interrupt ? index : -1
      }
    }
    return -1
  })()

  return (
    <div className={`app-shell ${sidebar ? '' : 'sidebar-closed'}`}>
      <ClarifyDialog
        interrupt={clarifyDialog}
        answers={clarifyAnswers}
        onAnswer={(id, value) => setClarifyAnswers((current) => ({ ...current, [id]: value }))}
        onClose={() => setClarifyDialog(null)}
        onSubmit={submitClarification}
        disabled={busy}
      />
      <PlanDialog
        interrupt={planDialog}
        feedback={planFeedback}
        onFeedback={setPlanFeedback}
        onClose={() => setPlanDialog(null)}
        onApprove={() => submitPlanDecision('approve')}
        onRevise={() => submitPlanDecision('revise')}
        onCancel={() => submitPlanDecision('cancel')}
        disabled={busy}
      />
      <aside className="sidebar">
        <div className="brand-row">
          <div className="brand"><span className="brand-mark"><Bot size={20} /></span><span>Agent Dogs</span></div>
          <button className="icon-button desktop-only" onClick={() => setSidebar(false)} aria-label="收起侧栏"><PanelLeftClose size={19} /></button>
        </div>
        <div className="workspace-switch">
          <button className={view === 'chat' ? 'active' : ''} onClick={() => setView('chat')}>会话</button>
          <button className={view === 'files' ? 'active' : ''} onClick={() => setView('files')}>文件</button>
        </div>

        {view === 'chat' ? (
          <>
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
          </>
        ) : (
          <>
            <div className="file-actions">
              <button title="刷新" onClick={refreshFiles}><RefreshCw size={15} /></button>
              <button title="新建文件" onClick={() => createFileItem('file')}><FilePlus size={15} /></button>
              <button title="新建文件夹" onClick={() => createFileItem('directory')}><FolderPlus size={15} /></button>
              <button title="上传" onClick={() => uploadInputRef.current?.click()}><Upload size={15} /></button>
              <button title="重命名" onClick={renameSelectedFile} disabled={!selectedFile?.path}><Edit3 size={15} /></button>
              <button title="删除" onClick={deleteSelectedFile} disabled={!selectedFile?.path}><Trash2 size={15} /></button>
              <input ref={uploadInputRef} type="file" onChange={uploadFile} hidden />
            </div>
            <div className="section-label">workspace</div>
            <div className="file-tree">
              {fileTree ? (
                <FileTreeNode
                  node={fileTree}
                  depth={0}
                  selectedPath={selectedFile?.path}
                  expandedDirs={expandedDirs}
                  onToggle={toggleDirectory}
                  onSelect={selectFileNode}
                />
              ) : <div className="file-empty">{fileBusy ? '正在加载文件...' : '暂无文件'}</div>}
            </div>
          </>
        )}

        <div className="sidebar-footer">
          <div className="model-status">
            <span className={`status-dot ${enabled.length ? 'online' : ''}`} />
            <div><strong>{enabled.length ? '模型已就绪' : '模型未配置'}</strong><small>{models.length ? `${models.length} 个可用模型` : enabled.map((item) => item.name).join(' · ') || '请检查设置'}</small></div>
          </div>
          <button className="settings-button"><Settings2 size={18} />设置<span>即将开放</span></button>
          <button
            type="button"
            className={`debug-toggle-button ${debugPanelEnabled ? 'active' : ''}`}
            onClick={toggleDebugPanel}
          >
            <Settings2 size={18} />
            调试信息
            <span>{debugPanelEnabled ? '显示中' : '已隐藏'}</span>
          </button>
        </div>
      </aside>

      {sidebar && <button className="mobile-scrim" onClick={() => setSidebar(false)} aria-label="关闭侧栏" />}
      <main className="chat-panel">
        <header className="topbar">
          <button className="icon-button" onClick={() => setSidebar(true)} aria-label="打开侧栏">{sidebar ? <ChevronLeft size={20} /> : <Menu size={20} />}</button>
          <div>
            <strong>{view === 'files' ? selectedFile?.name || '文件管理' : sessions.find((item) => item.id === activeId)?.title || '新会话'}</strong>
            <span>{view === 'files' ? selectedFile?.path || 'workspace' : '本地会话'}</span>
          </div>
        </header>
        {view === 'chat' ? (
          <>
            <section className="conversation">
              <div className="conversation-inner">
                {!messages.length ? <Welcome /> : messages.map((message, index) => (
                  <Message
                    key={`${index}-${message.role}`}
                    message={message}
                    onClarify={openClarification}
                    onPlan={openPlan}
                    canResume={index === latestPendingInterruptIndex}
                    showDebugPanel={debugPanelEnabled}
                  />
                ))}
                {busy && <div className="thinking"><span /><span /><span /> Agent 正在思考</div>}
                <div ref={bottomRef} />
              </div>
            </section>
            <div className="composer-wrap">
              {error && <div className="error-banner">{error}</div>}
              <form className="composer" onSubmit={submit}>
                <div className="composer-main">
                  <textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={onKeyDown} placeholder="给 Agent Dogs 发送消息" rows="1" />
                  <button
                    type={busy ? 'button' : 'submit'}
                    className={busy ? 'stop-send' : ''}
                    disabled={busy ? false : !input.trim()}
                    onClick={busy ? cancelCurrentRun : undefined}
                    aria-label={busy ? '中断当前回复' : '发送'}
                  >
                    {busy ? <StopCircle size={19} /> : <Send size={19} />}
                  </button>
                </div>
                <div className="composer-toolbar">
                  <select className="model-select" value={selectedModelKey} onChange={(event) => setSelectedModelKey(event.target.value)} disabled={!models.length} aria-label="选择模型">
                    {!models.length && <option value="">暂无可用模型</option>}
                    {groupedModels.map((group) => group.models.length ? (
                      <optgroup key={group.id} label={group.title}>
                        {group.models.map((model) => (
                          <option key={modelKey(model)} value={modelKey(model)}>
                            {model.display_name || model.model}
                          </option>
                        ))}
                      </optgroup>
                    ) : null)}
                  </select>
                  <div className="composer-controls">
                    <label className="temperature-control">
                      <span>温度 {Number(temperature).toFixed(1)}</span>
                      <input type="range" min="0" max="2" step="0.1" value={temperature} onChange={(event) => setTemperature(event.target.value)} />
                    </label>
                    <label className={`thinking-toggle ${thinkingEnabled ? 'active' : ''}`}>
                      <input type="checkbox" checked={thinkingEnabled} onChange={(event) => setThinkingEnabled(event.target.checked)} disabled={!selectedModel?.supports_thinking} />
                      <Brain size={15} />
                      深度思考
                    </label>
                  </div>
                </div>
              </form>
              <p className="composer-note">Enter 发送 · Shift + Enter 换行 · 模型输出可能存在错误，请核对重要信息</p>
            </div>
          </>
        ) : (
          <section className="file-panel">
            {fileError && <div className="error-banner file-error"><AlertTriangle size={15} />{fileError}</div>}
            {!selectedFile ? (
              <div className="file-placeholder">
                <FolderOpen size={42} />
                <h2>选择一个文件开始</h2>
                <p>左侧文件树显示 `workspace/` 目录。你可以新建、上传、重命名、删除和预览文件。</p>
              </div>
            ) : selectedFile.type === 'directory' ? (
              <div className="file-placeholder">
                <FolderOpen size={42} />
                <h2>{selectedFile.name}</h2>
                <p>{selectedFile.children?.length || 0} 个项目</p>
              </div>
            ) : (
              <div className="file-viewer">
                <div className="file-viewer-header">
                  <div>
                    <strong>{selectedFile.name}</strong>
                    <span>{formatBytes(selectedFile.size)} · {selectedFile.mime_type}</span>
                  </div>
                  <div className="file-viewer-actions">
                    {hasUnsavedFile && <span className="dirty-chip">未保存</span>}
                    {fileContent?.editable && <button onClick={saveCurrentFile} disabled={!hasUnsavedFile || fileBusy}><Save size={15} />保存</button>}
                    {fileContent?.editable && <button onClick={() => setDraftContent(fileContent.content)} disabled={!hasUnsavedFile}><X size={15} />取消</button>}
                    <a href={api.downloadFileUrl(selectedFile.path)} download><Download size={15} />下载</a>
                  </div>
                </div>
                {fileBusy && <div className="file-loading">正在处理文件...</div>}
                {fileContent?.editable ? (
                  <div className={`editor-grid ${isHtmlFile(selectedFile) ? 'with-preview' : ''}`}>
                    <textarea value={draftContent} onChange={(event) => setDraftContent(event.target.value)} spellCheck="false" />
                    {isHtmlFile(selectedFile) && <iframe title="HTML 预览" sandbox="" srcDoc={draftContent} />}
                  </div>
                ) : isDocxFile(selectedFile) && fileContent ? (
                  <pre className="text-preview">{fileContent.content || 'DOCX 没有可提取的文本内容。'}</pre>
                ) : isImageFile(selectedFile) ? (
                  <div className="binary-preview"><img src={api.rawFileUrl(selectedFile.path)} alt={selectedFile.name} /></div>
                ) : isPdfFile(selectedFile) ? (
                  <iframe className="document-preview" title={selectedFile.name} src={api.rawFileUrl(selectedFile.path)} />
                ) : (
                  <div className="file-placeholder compact">
                    <ImageIcon size={36} />
                    <h2>无法直接预览</h2>
                    <p>这个文件可以下载后用本地应用打开。</p>
                  </div>
                )}
              </div>
            )}
          </section>
        )}
      </main>
    </div>
  )
}
