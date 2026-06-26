/**
 * MessageBubble — a single chat message with role styling.
 *
 * role: "user" | "assistant"
 * Agent responses include FeedbackButtons and a Copy button if run_id is present.
 */

import { useState } from 'react'
import FeedbackButtons from './FeedbackButtons'

export default function MessageBubble({ role, content, runId, failureReason }) {
  const isUser = role === 'user'
  const isFailed = !isUser && Boolean(failureReason)
  const [copied, setCopied] = useState(false)

  function handleCopy() {
    navigator.clipboard.writeText(content).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: isUser ? 'flex-end' : 'flex-start',
      marginBottom: 12,
    }}>
      <div style={{
        maxWidth: '72%',
        background: isUser ? '#6366f1' : isFailed ? '#fff8e6' : '#f0f0f0',
        color: isUser ? '#fff' : isFailed ? '#7c4a00' : '#111',
        border: isFailed ? '1px solid #f0a500' : 'none',
        padding: '10px 14px',
        borderRadius: isUser ? '14px 14px 4px 14px' : '14px 14px 14px 4px',
        fontFamily: 'monospace',
        fontSize: 14,
        fontStyle: isFailed ? 'italic' : 'normal',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        lineHeight: 1.5,
      }}>
        {content}
      </div>
      {!isUser && (
        <div style={{ marginTop: 2, display: 'flex', alignItems: 'center', gap: 4 }}>
          {runId && <FeedbackButtons runId={runId} />}
          <button
            onClick={handleCopy}
            style={{
              background: 'none',
              border: 'none',
              cursor: 'pointer',
              fontSize: 11,
              color: copied ? '#22c55e' : '#999',
              fontFamily: 'monospace',
              padding: '2px 6px',
            }}
          >
            {copied ? 'Copied!' : 'Copy'}
          </button>
        </div>
      )}
    </div>
  )
}
