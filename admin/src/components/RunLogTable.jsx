/**
 * RunLogTable — paginated, filterable table of run records from /admin/runs.
 *
 * Columns: timestamp, session_id (truncated), latency_ms, feedback_signal,
 * tool_calls count, activated_mu count, activated_edge count.
 * Clicking a row expands it to show the full run_id and weight_updates.
 */

import { useState, useEffect, useCallback } from 'react'
import { fetchRuns } from '../api'

const PAGE_SIZE = 20

export default function RunLogTable() {
  const [runs, setRuns] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [offset, setOffset] = useState(0)
  const [sessionFilter, setSessionFilter] = useState('')
  const [expandedRunId, setExpandedRunId] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await fetchRuns({
        sessionId: sessionFilter || undefined,
        limit: PAGE_SIZE,
        offset,
      })
      setRuns(data.runs || [])
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [sessionFilter, offset])

  useEffect(() => { load() }, [load])

  function toggleExpand(runId) {
    setExpandedRunId(prev => (prev === runId ? null : runId))
  }

  function feedbackLabel(signal) {
    if (signal === 1) return '👍 +1'
    if (signal === -1) return '👎 -1'
    return '—'
  }

  return (
    <div style={{ fontFamily: 'monospace', fontSize: 13 }}>
      <div style={{ marginBottom: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
        <input
          placeholder="Filter by session ID…"
          value={sessionFilter}
          onChange={e => { setSessionFilter(e.target.value); setOffset(0) }}
          style={{ padding: '4px 8px', width: 320, fontFamily: 'monospace' }}
        />
        <button onClick={load} style={{ padding: '4px 10px' }}>Refresh</button>
      </div>

      {error && <p style={{ color: 'red' }}>Error: {error}</p>}
      {loading && <p>Loading…</p>}

      {!loading && !error && (
        <>
          <table style={{ borderCollapse: 'collapse', width: '100%' }}>
            <thead>
              <tr style={{ background: '#f0f0f0' }}>
                {['Timestamp', 'Session', 'Latency ms', 'Feedback', 'Tools', 'Act. MUs', 'Act. Edges'].map(h => (
                  <th key={h} style={{ border: '1px solid #ccc', padding: '4px 8px', textAlign: 'left' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {runs.map(run => (
                <>
                  <tr
                    key={run.run_id}
                    onClick={() => toggleExpand(run.run_id)}
                    style={{ cursor: 'pointer', background: expandedRunId === run.run_id ? '#e8f0fe' : 'white' }}
                  >
                    <td style={tdStyle}>{run.timestamp?.slice(0, 19).replace('T', ' ')}</td>
                    <td style={tdStyle}>{run.session_id?.slice(0, 8)}…</td>
                    <td style={tdStyle}>{run.latency_ms ?? '—'}</td>
                    <td style={tdStyle}>{feedbackLabel(run.feedback_signal)}</td>
                    <td style={tdStyle}>{(run.tool_calls || []).length}</td>
                    <td style={tdStyle}>{(run.activated_mu_ids || []).length}</td>
                    <td style={tdStyle}>{(run.activated_edge_ids || []).length}</td>
                  </tr>
                  {expandedRunId === run.run_id && (
                    <tr key={`${run.run_id}-detail`}>
                      <td colSpan={7} style={{ padding: '8px 12px', background: '#f8f8f8', border: '1px solid #ccc' }}>
                        <strong>run_id:</strong> {run.run_id}<br />
                        <strong>input_mu_id:</strong> {run.input_mu_id}<br />
                        <strong>output_mu_id:</strong> {run.output_mu_id}<br />
                        <strong>algorithm:</strong> {run.algorithm}<br />
                        <strong>session_ctx:</strong> {run.use_session_context ? 'on' : 'off'}<br />
                        {run.weight_updates?.length > 0 && (
                          <>
                            <strong>weight_updates:</strong>
                            <ul style={{ margin: '4px 0', paddingLeft: 16 }}>
                              {run.weight_updates.map((u, i) => (
                                <li key={i}>
                                  {u.edge_id?.slice(0, 8)}… &nbsp;
                                  {u.old_weight?.toFixed(4)} → {u.new_weight?.toFixed(4)}
                                </li>
                              ))}
                            </ul>
                          </>
                        )}
                      </td>
                    </tr>
                  )}
                </>
              ))}
              {runs.length === 0 && (
                <tr><td colSpan={7} style={{ padding: 12, textAlign: 'center', color: '#888' }}>No runs yet.</td></tr>
              )}
            </tbody>
          </table>

          <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
            <button onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} disabled={offset === 0}>
              ← Prev
            </button>
            <span style={{ lineHeight: '28px' }}>Offset: {offset}</span>
            <button onClick={() => setOffset(offset + PAGE_SIZE)} disabled={runs.length < PAGE_SIZE}>
              Next →
            </button>
          </div>
        </>
      )}
    </div>
  )
}

const tdStyle = { border: '1px solid #ccc', padding: '4px 8px' }
