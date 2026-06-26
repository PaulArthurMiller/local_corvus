/**
 * Probe Results view — per-phenomenon learning curves and result table.
 *
 * Fetches from GET /admin/probe-results with optional filters.
 * Shows: filter controls, results table, per-phenomenon rank curve.
 */

import { useState, useEffect } from 'react'

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

const PHENOMENON_COLORS = {
  ranking_improvement: '#2196F3',
  cross_cluster_transfer: '#4CAF50',
  process_rule_adherence: '#FF9800',
}

function mean(values) {
  if (!values.length) return null
  return values.reduce((a, b) => a + b, 0) / values.length
}

export default function ProbeResults() {
  const [results, setResults] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  // Filter state
  const [filterProbeId, setFilterProbeId] = useState('')
  const [filterCondition, setFilterCondition] = useState('')
  const [filterSession, setFilterSession] = useState('')
  const [bridgeOnly, setBridgeOnly] = useState(false)

  const fetchResults = async () => {
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams()
      if (filterProbeId) params.set('probe_id', filterProbeId)
      if (filterCondition) params.set('condition_label', filterCondition)
      if (filterSession) params.set('measurement_session_index', filterSession)

      const resp = await fetch(`${API_URL}/admin/probe-results?${params}`)
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json()
      setResults(data.probe_results || [])
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchResults() }, [])

  // Derive unique phenomena from results for the learning curve
  const phenomena = [...new Set(results.map(r => r.phenomenon_type).filter(Boolean))]

  // Build per-phenomenon mean gold_mu_rank by measurement_session_index
  const curveData = phenomena.map(phen => {
    const phenResults = results.filter(r =>
      r.phenomenon_type === phen && r.gold_mu_rank != null
    )
    const bySession = {}
    for (const r of phenResults) {
      const idx = r.measurement_session_index
      if (!bySession[idx]) bySession[idx] = []
      bySession[idx].push(r.gold_mu_rank)
    }
    const points = Object.entries(bySession)
      .map(([idx, ranks]) => ({ session: Number(idx), meanRank: mean(ranks) }))
      .sort((a, b) => a.session - b.session)
    return { phenomenon: phen, points }
  })

  const displayResults = bridgeOnly
    ? results.filter(r => r.is_bridge_probe)
    : results

  const cellStyle = {
    padding: '6px 10px',
    borderBottom: '1px solid #eee',
    fontFamily: 'monospace',
    fontSize: 12,
  }
  const headerStyle = {
    ...cellStyle,
    fontWeight: 'bold',
    background: '#f5f5f5',
    borderBottom: '2px solid #ccc',
  }

  return (
    <div>
      <h2 style={{ fontSize: 15, marginBottom: 12 }}>Probe Results</h2>

      {/* Filters */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        <input
          placeholder="probe_id filter"
          value={filterProbeId}
          onChange={e => setFilterProbeId(e.target.value)}
          style={{ fontFamily: 'monospace', fontSize: 12, padding: '4px 8px', width: 120 }}
        />
        <input
          placeholder="condition filter"
          value={filterCondition}
          onChange={e => setFilterCondition(e.target.value)}
          style={{ fontFamily: 'monospace', fontSize: 12, padding: '4px 8px', width: 140 }}
        />
        <input
          placeholder="meas. session idx"
          value={filterSession}
          onChange={e => setFilterSession(e.target.value)}
          type="number"
          style={{ fontFamily: 'monospace', fontSize: 12, padding: '4px 8px', width: 100 }}
        />
        <label style={{ fontFamily: 'monospace', fontSize: 12, cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={bridgeOnly}
            onChange={e => setBridgeOnly(e.target.checked)}
            style={{ marginRight: 4 }}
          />
          Bridge probes only
        </label>
        <button
          onClick={fetchResults}
          style={{ fontFamily: 'monospace', fontSize: 12, padding: '4px 12px', cursor: 'pointer' }}
        >
          Apply
        </button>
      </div>

      {error && (
        <div style={{ color: 'red', fontFamily: 'monospace', fontSize: 12, marginBottom: 8 }}>
          Error: {error}
        </div>
      )}

      {/* Per-phenomenon learning curves (ASCII-style rank chart) */}
      {curveData.length > 0 && (
        <div style={{ marginBottom: 20 }}>
          <h3 style={{ fontSize: 13, marginBottom: 8 }}>Learning Curves — Mean Gold MU Rank by Session</h3>
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
            {curveData.map(({ phenomenon, points }) => (
              <div key={phenomenon} style={{ border: '1px solid #ddd', padding: 12, minWidth: 260 }}>
                <div style={{
                  fontFamily: 'monospace',
                  fontSize: 11,
                  fontWeight: 'bold',
                  color: PHENOMENON_COLORS[phenomenon] || '#333',
                  marginBottom: 6,
                }}>
                  {phenomenon}
                </div>
                {points.length === 0 ? (
                  <div style={{ fontFamily: 'monospace', fontSize: 11, color: '#999' }}>
                    No data with gold MU rank
                  </div>
                ) : (
                  <table style={{ borderCollapse: 'collapse', width: '100%' }}>
                    <thead>
                      <tr>
                        <th style={{ ...headerStyle, textAlign: 'left' }}>Session</th>
                        <th style={{ ...headerStyle, textAlign: 'right' }}>Mean Rank</th>
                      </tr>
                    </thead>
                    <tbody>
                      {points.map(pt => (
                        <tr key={pt.session}>
                          <td style={cellStyle}>{pt.session}</td>
                          <td style={{ ...cellStyle, textAlign: 'right' }}>
                            {pt.meanRank != null ? pt.meanRank.toFixed(2) : '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Results table */}
      <h3 style={{ fontSize: 13, marginBottom: 8 }}>
        {loading ? 'Loading…' : `${displayResults.length} result(s)`}
      </h3>

      {!loading && displayResults.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ borderCollapse: 'collapse', width: '100%', fontSize: 12 }}>
            <thead>
              <tr>
                {['probe_id', 'phenomenon', 'session', 'condition', 'rank', 'precision', 'recall', 'bridge'].map(h => (
                  <th key={h} style={headerStyle}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {displayResults.map(r => (
                <tr key={r.result_id}>
                  <td style={cellStyle}>{r.probe_id}</td>
                  <td style={cellStyle}>{r.phenomenon_type || '—'}</td>
                  <td style={cellStyle}>{r.measurement_session_index}</td>
                  <td style={cellStyle}>{r.condition_label}</td>
                  <td style={cellStyle}>{r.gold_mu_rank ?? '—'}</td>
                  <td style={cellStyle}>
                    {r.retrieval_precision != null ? r.retrieval_precision.toFixed(3) : '—'}
                  </td>
                  <td style={cellStyle}>
                    {r.retrieval_recall != null ? r.retrieval_recall.toFixed(3) : '—'}
                  </td>
                  <td style={cellStyle}>{r.is_bridge_probe ? 'yes' : 'no'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!loading && displayResults.length === 0 && !error && (
        <div style={{ fontFamily: 'monospace', fontSize: 12, color: '#999' }}>
          No probe results. Run probes via POST /admin/run-probe.
        </div>
      )}
    </div>
  )
}
