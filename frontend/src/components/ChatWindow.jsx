/**
 * ChatWindow — the full chat interface.
 *
 * Manages message history, session ID, and submission state.
 * Each assistant response carries a run_id for feedback submission.
 */

import { useState, useRef, useEffect } from 'react'
import MessageBubble from './MessageBubble'
import { sendMessage } from '../api'

export default function ChatWindow() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [sessionId, setSessionId] = useState(null)
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function handleSubmit(e) {
    e.preventDefault()
    const text = input.trim()
    if (!text || loading) return

    setInput('')
    setError(null)
    setMessages(prev => [...prev, { role: 'user', content: text }])
    setLoading(true)

    console.log(`[ChatWindow] sending message session_id=${sessionId} len=${text.length}`)

    try {
      const data = await sendMessage(text, sessionId)
      console.log(`[ChatWindow] response received run_id=${data.run_id} session_id=${data.session_id}`)
      if (!sessionId) setSessionId(data.session_id)
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

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {sessionId && (
        <div style={{ fontSize: 11, color: '#888', marginBottom: 4, fontFamily: 'monospace' }}>
          session: {sessionId}
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

      <form onSubmit={handleSubmit} style={{ display: 'flex', gap: 8, marginTop: 8 }}>
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
        <button
          type="submit"
          disabled={loading || !input.trim()}
          style={{
            padding: '0 20px',
            background: '#6366f1',
            color: '#fff',
            border: 'none',
            borderRadius: 6,
            cursor: loading || !input.trim() ? 'default' : 'pointer',
            opacity: loading || !input.trim() ? 0.6 : 1,
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
