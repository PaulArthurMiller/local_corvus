/**
 * API client for the Local Corvus backend.
 */

const BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

export async function sendMessage(message, sessionId, attachments = []) {
  const body = { message, session_id: sessionId || undefined }
  if (attachments.length > 0) {
    // Strip preview_url — it's UI-only state, not part of the API contract
    body.attachments = attachments.map(({ filename, mime_type, data_b64 }) => ({
      filename,
      mime_type,
      data_b64,
    }))
  }
  const res = await fetch(`${BASE_URL}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`POST /chat → ${res.status}: ${text}`)
  }
  return res.json()
}

export async function sendFeedback(runId, signal) {
  const res = await fetch(`${BASE_URL}/feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ run_id: runId, signal }),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`POST /feedback → ${res.status}: ${text}`)
  }
  return res.json()
}
