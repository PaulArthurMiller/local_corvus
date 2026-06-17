/**
 * FeedbackButtons — thumbs up / thumbs down for a single agent response.
 *
 * Fires POST /feedback with run_id and signal (+1 or -1).
 * Disables after one click to prevent double-signaling the same run.
 */

import { useState } from 'react'
import { sendFeedback } from '../api'

export default function FeedbackButtons({ runId }) {
  const [submitted, setSubmitted] = useState(null)  // null | 1 | -1
  const [error, setError] = useState(null)

  async function handleFeedback(signal) {
    if (submitted !== null || !runId) return
    try {
      await sendFeedback(runId, signal)
      setSubmitted(signal)
      console.log(`[FeedbackButtons] feedback submitted run_id=${runId} signal=${signal}`)
    } catch (err) {
      setError(err.message)
      console.error('[FeedbackButtons] feedback error:', err)
    }
  }

  if (!runId) return null

  return (
    <div style={{ display: 'inline-flex', gap: 4, marginTop: 4 }}>
      <button
        onClick={() => handleFeedback(1)}
        disabled={submitted !== null}
        title="Helpful"
        style={{
          background: 'none',
          border: '1px solid #ccc',
          borderRadius: 4,
          padding: '2px 8px',
          cursor: submitted !== null ? 'default' : 'pointer',
          opacity: submitted !== null && submitted !== 1 ? 0.3 : 1,
          fontSize: 16,
        }}
      >
        👍
      </button>
      <button
        onClick={() => handleFeedback(-1)}
        disabled={submitted !== null}
        title="Not helpful"
        style={{
          background: 'none',
          border: '1px solid #ccc',
          borderRadius: 4,
          padding: '2px 8px',
          cursor: submitted !== null ? 'default' : 'pointer',
          opacity: submitted !== null && submitted !== -1 ? 0.3 : 1,
          fontSize: 16,
        }}
      >
        👎
      </button>
      {error && <span style={{ color: 'red', fontSize: 11 }}>{error}</span>}
    </div>
  )
}
