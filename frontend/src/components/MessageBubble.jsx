/**
 * MessageBubble — a single chat message with role styling.
 *
 * role: "user" | "assistant"
 * Agent responses include FeedbackButtons if run_id is present.
 */

import FeedbackButtons from './FeedbackButtons'

export default function MessageBubble({ role, content, runId }) {
  const isUser = role === 'user'

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: isUser ? 'flex-end' : 'flex-start',
      marginBottom: 12,
    }}>
      <div style={{
        maxWidth: '72%',
        background: isUser ? '#6366f1' : '#f0f0f0',
        color: isUser ? '#fff' : '#111',
        padding: '10px 14px',
        borderRadius: isUser ? '14px 14px 4px 14px' : '14px 14px 14px 4px',
        fontFamily: 'monospace',
        fontSize: 14,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        lineHeight: 1.5,
      }}>
        {content}
      </div>
      {!isUser && runId && (
        <div style={{ marginTop: 2 }}>
          <FeedbackButtons runId={runId} />
        </div>
      )}
    </div>
  )
}
