/**
 * ChatWindow — the full chat interface.
 *
 * Manages message history, session ID, submission state, and token tracking.
 * Each assistant response carries a run_id for feedback submission.
 */

import { useState, useRef, useEffect } from 'react'
import MessageBubble from './MessageBubble'
import { sendMessage } from '../api'

const ALLOWED_MIME_TYPES = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']

const DEFAULT_CONTEXT_WINDOW = 8192

export default function ChatWindow() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [sessionId, setSessionId] = useState(null)
  const [sessionTokensIn, setSessionTokensIn] = useState(0)
  const [sessionTokensOut, setSessionTokensOut] = useState(0)
  const [lastPromptTokens, setLastPromptTokens] = useState(null)
  const [contextWindowSize, setContextWindowSize] = useState(DEFAULT_CONTEXT_WINDOW)
  const [attachments, setAttachments] = useState([])
  const fileInputRef = useRef(null)
  const bottomRef = useRef(null)

  // Fetch context window size from config once on mount
  useEffect(() => {
    fetch('/admin/config')
      .then(r => r.ok ? r.json() : null)
      .then(cfg => {
        const size = cfg?.model?.context_window_tokens
        if (size != null) setContextWindowSize(size)
      })
      .catch(() => {/* keep default */})
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  function handleFileSelect(e) {
    const files = Array.from(e.target.files || [])
    e.target.value = ''  // allow re-selecting the same file
    if (!files.length) return

    files.forEach(file => {
      if (!ALLOWED_MIME_TYPES.includes(file.type)) {
        console.warn(`[ChatWindow] rejected file ${file.name}: unsupported type ${file.type}`)
        return
      }
      const preview_url = URL.createObjectURL(file)
      const reader = new FileReader()
      reader.onload = (ev) => {
        const dataUrl = ev.target.result
        // dataUrl format: "data:<mime>;base64,<data>" — extract just the base64 part
        const data_b64 = dataUrl.slice(dataUrl.indexOf(',') + 1)
        setAttachments(prev => [...prev, {
          filename: file.name,
          mime_type: file.type,
          data_b64,
          preview_url,
        }])
        console.log(`[ChatWindow] attachment queued: ${file.name} (${file.type})`)
      }
      reader.readAsDataURL(file)
    })
  }

  function removeAttachment(index) {
    setAttachments(prev => {
      URL.revokeObjectURL(prev[index].preview_url)
      return prev.filter((_, i) => i !== index)
    })
  }

  async function handleSubmit(e) {
    e.preventDefault()
    const text = input.trim()
    if ((!text && attachments.length === 0) || loading) return

    const pendingAttachments = [...attachments]
    setInput('')
    setAttachments([])
    setError(null)
    setMessages(prev => [...prev, { role: 'user', content: text || '[image]' }])
    setLoading(true)

    console.log(`[ChatWindow] sending message session_id=${sessionId} len=${text.length} attachments=${pendingAttachments.length}`)

    try {
      const data = await sendMessage(text, sessionId, pendingAttachments)
      // Revoke object URLs now that the send succeeded
      pendingAttachments.forEach(att => URL.revokeObjectURL(att.preview_url))
      console.log(`[ChatWindow] response received run_id=${data.run_id} session_id=${data.session_id} tokens_in=${data.tokens_in} tokens_out=${data.tokens_out}`)
      if (!sessionId) setSessionId(data.session_id)

      if (data.tokens_in != null) {
        setSessionTokensIn(prev => prev + data.tokens_in)
        setLastPromptTokens(data.tokens_in)
      }
      if (data.tokens_out != null) {
        setSessionTokensOut(prev => prev + data.tokens_out)
      }

      setMessages(prev => [...prev, {
        role: 'assistant',
        content: data.response,
        runId: data.run_id,
      }])
    } catch (err) {
      console.error('[ChatWindow] error:', err)
      setError(err.message)
      setMessages(prev => [...prev, {
        role: 'assistant',
        content: `[Error: ${err.message}]`,
        runId: null,
      }])
    } finally {
      setLoading(false)
    }
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      handleSubmit(e)
    }
  }

  function handleNewSession() {
    attachments.forEach(att => URL.revokeObjectURL(att.preview_url))
    setAttachments([])
    setMessages([])
    setSessionId(null)
    setSessionTokensIn(0)
    setSessionTokensOut(0)
    setLastPromptTokens(null)
    setError(null)
    setInput('')
    console.log('[ChatWindow] new session started')
  }

  const contextPct = lastPromptTokens != null
    ? Math.round(lastPromptTokens / contextWindowSize * 100)
    : null

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {sessionId && (
        <div style={{ fontSize: 11, color: '#888', marginBottom: 2, fontFamily: 'monospace' }}>
          session: {sessionId}
        </div>
      )}

      {lastPromptTokens != null && (
        <div style={{ fontSize: 11, color: '#888', marginBottom: 6, fontFamily: 'monospace' }}>
          context: {contextPct}% ({lastPromptTokens.toLocaleString()} / {contextWindowSize.toLocaleString()})
          {'   '}session: {sessionTokensIn.toLocaleString()} in / {sessionTokensOut.toLocaleString()} out
        </div>
      )}

      <div style={{
        flex: 1,
        overflowY: 'auto',
        padding: '8px 0',
        minHeight: 0,
      }}>
        {messages.length === 0 && (
          <p style={{ color: '#888', textAlign: 'center', marginTop: 40, fontFamily: 'monospace' }}>
            Send a message to start.
          </p>
        )}
        {messages.map((msg, i) => (
          <MessageBubble key={i} role={msg.role} content={msg.content} runId={msg.runId} />
        ))}
        {loading && (
          <div style={{ fontFamily: 'monospace', color: '#888', fontSize: 13, marginBottom: 12 }}>
            thinking…
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {error && <p style={{ color: 'red', fontSize: 12, fontFamily: 'monospace' }}>{error}</p>}

      {/* Attachment preview strip */}
      {attachments.length > 0 && (
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 6 }}>
          {attachments.map((att, i) => (
            <div key={i} style={{
              position: 'relative',
              border: '1px solid #ccc',
              borderRadius: 4,
              overflow: 'hidden',
              width: 64,
              height: 64,
              flexShrink: 0,
            }}>
              <img
                src={att.preview_url}
                alt={att.filename}
                title={att.filename}
                style={{ width: '100%', height: '100%', objectFit: 'cover' }}
              />
              <button
                type="button"
                onClick={() => removeAttachment(i)}
                title="Remove"
                style={{
                  position: 'absolute', top: 1, right: 1,
                  background: 'rgba(0,0,0,0.55)', color: '#fff',
                  border: 'none', borderRadius: 3,
                  width: 18, height: 18, fontSize: 11,
                  cursor: 'pointer', lineHeight: '18px', textAlign: 'center',
                  padding: 0,
                }}
              >×</button>
            </div>
          ))}
        </div>
      )}

      <form onSubmit={handleSubmit} style={{ display: 'flex', gap: 8, marginTop: 8 }}>
        {/* Hidden file input */}
        <input
          type="file"
          accept="image/jpeg,image/png,image/gif,image/webp"
          multiple
          ref={fileInputRef}
          onChange={handleFileSelect}
          style={{ display: 'none' }}
        />
        <textarea
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Type a message… (Enter to send, Shift+Enter for newline)"
          rows={3}
          style={{
            flex: 1,
            padding: '8px 10px',
            fontFamily: 'monospace',
            fontSize: 14,
            resize: 'vertical',
            border: '1px solid #ccc',
            borderRadius: 6,
          }}
          disabled={loading}
        />
        {/* Paperclip button */}
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={loading}
          title="Attach image"
          style={{
            padding: '0 12px',
            background: 'transparent',
            color: '#888',
            border: '1px solid #ccc',
            borderRadius: 6,
            cursor: loading ? 'default' : 'pointer',
            fontSize: 18,
          }}
        >📎</button>
        <button
          type="button"
          onClick={handleNewSession}
          disabled={loading}
          style={{
            padding: '0 16px',
            background: 'transparent',
            color: '#666',
            border: '1px solid #ccc',
            borderRadius: 6,
            cursor: loading ? 'default' : 'pointer',
            fontFamily: 'monospace',
            fontSize: 13,
          }}
        >
          New Session
        </button>
        <button
          type="submit"
          disabled={loading || (!input.trim() && attachments.length === 0)}
          style={{
            padding: '0 20px',
            background: '#6366f1',
            color: '#fff',
            border: 'none',
            borderRadius: 6,
            cursor: loading || (!input.trim() && attachments.length === 0) ? 'default' : 'pointer',
            opacity: loading || (!input.trim() && attachments.length === 0) ? 0.6 : 1,
            fontFamily: 'monospace',
            fontWeight: 'bold',
          }}
        >
          Send
        </button>
      </form>
    </div>
  )
}
