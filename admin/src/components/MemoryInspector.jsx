/**
 * MemoryInspector — browse the MU graph, view edge weights, click to trace MU history.
 *
 * Displays MUs in a paginated table sorted by activation_count DESC.
 * Clicking a MU expands it to show raw_content and all connected edges with weights.
 * A depth limit of 1 hop is enforced on the edge display (not the full graph) to
 * keep the view usable as MU counts grow.
 */

import { useEffect, useState, useCallback } from 'react'
import { fetchGraph } from '../api'

const MU_PAGE_SIZE = 30

export default function MemoryInspector() {
  const [mus, setMus] = useState([])
  const [edges, setEdges] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [selectedMuId, setSelectedMuId] = useState(null)
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await fetchGraph()
      const sorted = (data.memory_units || []).slice().sort(
        (a, b) => (b.activation_count || 0) - (a.activation_count || 0)
      )
      setMus(sorted)
      setEdges(data.edges || [])
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const filtered = search
    ? mus.filter(m =>
        m.raw_content?.toLowerCase().includes(search.toLowerCase()) ||
        m.mu_id?.toLowerCase().includes(search.toLowerCase()) ||
        m.source_type?.toLowerCase().includes(search.toLowerCase())
      )
    : mus

  const paginated = filtered.slice(page * MU_PAGE_SIZE, (page + 1) * MU_PAGE_SIZE)

  function edgesForMu(muId) {
    return edges.filter(e => e.source_mu_id === muId || e.target_mu_id === muId)
  }

  function sourceTypeColor(type) {
    const map = {
      user_input: '#3b82f6',
      agent_output: '#10b981',
      prompt: '#8b5cf6',
      tool_call: '#f59e0b',
      tool_result: '#ef4444',
      system: '#6b7280',
    }
    return map[type] || '#374151'
  }

  return (
    <div style={{ fontFamily: 'monospace', fontSize: 13 }}>
      <div style={{ marginBottom: 10, display: 'flex', gap: 8, alignItems: 'center' }}>
        <input
          placeholder="Search content, mu_id, or source_type…"
          value={search}
          onChange={e => { setSearch(e.target.value); setPage(0) }}
          style={{ padding: '4px 8px', width: 360, fontFamily: 'monospace' }}
        />
        <button onClick={load} style={{ padding: '4px 10px' }}>Refresh</button>
        <span style={{ color: '#555' }}>{filtered.length} MU(s) | {edges.length} edge(s)</span>
      </div>

      {error && <p style={{ color: 'red' }}>Error: {error}</p>}
      {loading && <p>Loading…</p>}

      {!loading && !error && (
        <>
          <table style={{ borderCollapse: 'collapse', width: '100%' }}>
            <thead>
              <tr style={{ background: '#f0f0f0' }}>
                {['Source type', 'mu_id', 'Created', 'Activations', 'Preview'].map(h => (
                  <th key={h} style={{ border: '1px solid #ccc', padding: '4px 8px', textAlign: 'left' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {paginated.map(mu => (
                <>
                  <tr
                    key={mu.mu_id}
                    onClick={() => setSelectedMuId(prev => prev === mu.mu_id ? null : mu.mu_id)}
                    style={{ cursor: 'pointer', background: selectedMuId === mu.mu_id ? '#e8f0fe' : 'white' }}
                  >
                    <td style={tdStyle}>
                      <span style={{ color: sourceTypeColor(mu.source_type), fontWeight: 'bold' }}>
                        {mu.source_type}
                      </span>
                    </td>
                    <td style={tdStyle}>{mu.mu_id?.slice(0, 8)}…</td>
                    <td style={tdStyle}>{mu.created_at?.slice(0, 19).replace('T', ' ')}</td>
                    <td style={tdStyle}>{mu.activation_count ?? 0}</td>
                    <td style={{ ...tdStyle, maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {mu.raw_content?.slice(0, 80)}
                    </td>
                  </tr>
                  {selectedMuId === mu.mu_id && (
                    <tr key={`${mu.mu_id}-detail`}>
                      <td colSpan={5} style={{ padding: '10px 14px', background: '#f8f8f8', border: '1px solid #ccc' }}>
                        <strong>mu_id:</strong> {mu.mu_id}<br />
                        <strong>series_id:</strong> {mu.series_id || '—'}<br />
                        <strong>experiment_id:</strong> {mu.experiment_id || '—'}<br />
                        <strong>cluster_id:</strong> {mu.cluster_id || '—'}<br />
                        <strong>source_type:</strong> {mu.source_type}<br />
                        <strong>created_at:</strong> {mu.created_at}<br />
                        <strong>last_activated_at:</strong> {mu.last_activated_at || '—'}<br />
                        <strong>activation_count:</strong> {mu.activation_count}<br />
                        <strong>pruning_flag:</strong> {mu.pruning_flag}<br />
                        <br />
                        <strong>raw_content:</strong>
                        <pre style={{ whiteSpace: 'pre-wrap', margin: '4px 0', background: '#fff', padding: 8, border: '1px solid #e0e0e0', maxHeight: 200, overflow: 'auto' }}>
                          {mu.raw_content}
                        </pre>
                        <strong>Connected edges ({edgesForMu(mu.mu_id).length}):</strong>
                        {edgesForMu(mu.mu_id).length === 0
                          ? <span style={{ color: '#888' }}> none</span>
                          : (
                            <ul style={{ margin: '4px 0', paddingLeft: 16 }}>
                              {edgesForMu(mu.mu_id).map(e => (
                                <li key={e.edge_id}>
                                  <span style={{ color: '#666' }}>{e.edge_id.slice(0, 8)}…</span>
                                  &nbsp; {e.source_mu_id.slice(0, 8)}… → {e.target_mu_id.slice(0, 8)}…
                                  &nbsp; weight=<strong>{e.weight?.toFixed(4)}</strong>
                                  &nbsp; traversals={e.traversal_count}
                                  &nbsp; dir={e.direction}
                                </li>
                              ))}
                            </ul>
                          )
                        }
                      </td>
                    </tr>
                  )}
                </>
              ))}
              {paginated.length === 0 && (
                <tr><td colSpan={5} style={{ padding: 12, textAlign: 'center', color: '#888' }}>No memory units found.</td></tr>
              )}
            </tbody>
          </table>

          <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
            <button onClick={() => setPage(p => Math.max(0, p - 1))} disabled={page === 0}>← Prev</button>
            <span style={{ lineHeight: '28px' }}>Page {page + 1}</span>
            <button onClick={() => setPage(p => p + 1)} disabled={(page + 1) * MU_PAGE_SIZE >= filtered.length}>Next →</button>
          </div>
        </>
      )}
    </div>
  )
}

const tdStyle = { border: '1px solid #ccc', padding: '4px 8px' }
