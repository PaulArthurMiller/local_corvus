/**
 * ReportsList — filterable list of generated reports with view/download actions.
 *
 * Fetches from GET /admin/reports with optional type and experiment_id filters.
 * Calls onSelect(reportId) when a report is chosen for viewing.
 */

import { useState, useEffect } from 'react'

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

const cellStyle = { padding: '6px 10px', textAlign: 'left', borderBottom: '1px solid #eee' }
const headStyle = { ...cellStyle, fontWeight: 'bold', background: '#f5f5f5' }

export default function ReportsList({ onSelect, selectedId }) {
  const [reports, setReports] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  // Filter state
  const [filterType, setFilterType] = useState('')
  const [filterExperiment, setFilterExperiment] = useState('')

  const fetchReports = async () => {
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams()
      if (filterType) params.set('report_type', filterType)
      if (filterExperiment) params.set('experiment_id', filterExperiment)
      const resp = await fetch(`${API_URL}/admin/reports?${params}`)
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json()
      setReports(data.reports || [])
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { fetchReports() }, [])

  const handleDownloadJson = async (reportId, e) => {
    e.stopPropagation()
    try {
      const resp = await fetch(`${API_URL}/admin/reports/${reportId}`)
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json()
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `report_${reportId.slice(0, 8)}.json`
      a.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      alert(`Download failed: ${err.message}`)
    }
  }

  const inputStyle = {
    fontFamily: 'monospace', fontSize: 12, padding: '4px 8px',
    border: '1px solid #ccc', borderRadius: 3, marginRight: 8,
  }
  const btnStyle = {
    fontFamily: 'monospace', fontSize: 12, padding: '4px 10px',
    cursor: 'pointer', border: '1px solid #999', borderRadius: 3, background: '#f0f0f0',
  }

  return (
    <div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap', alignItems: 'center' }}>
        <select
          value={filterType}
          onChange={e => setFilterType(e.target.value)}
          style={inputStyle}
        >
          <option value="">All types</option>
          <option value="series">series</option>
          <option value="experiment">experiment</option>
        </select>
        <input
          placeholder="experiment_id filter…"
          value={filterExperiment}
          onChange={e => setFilterExperiment(e.target.value)}
          style={{ ...inputStyle, width: 200 }}
        />
        <button onClick={fetchReports} style={btnStyle}>Refresh</button>
        <span style={{ color: '#777', fontSize: 12 }}>
          {loading ? 'Loading…' : `${reports.length} report(s)`}
        </span>
      </div>

      {error && (
        <div style={{ color: '#c00', marginBottom: 8, fontSize: 12 }}>Error: {error}</div>
      )}

      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <thead>
          <tr>
            <th style={headStyle}>Type</th>
            <th style={headStyle}>Experiment</th>
            <th style={headStyle}>Series</th>
            <th style={headStyle}>Generated</th>
            <th style={headStyle}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {reports.length === 0 && !loading && (
            <tr>
              <td colSpan={5} style={{ ...cellStyle, color: '#888', fontStyle: 'italic' }}>
                No reports found. Generate one using the form below.
              </td>
            </tr>
          )}
          {reports.map(r => (
            <tr
              key={r.report_id}
              style={{
                cursor: 'pointer',
                background: selectedId === r.report_id ? '#e8f4ff' : 'transparent',
              }}
              onClick={() => onSelect && onSelect(r.report_id)}
            >
              <td style={cellStyle}>
                <span style={{
                  padding: '2px 6px', borderRadius: 3, fontSize: 11,
                  background: r.report_type === 'experiment' ? '#e3f0fb' : '#f0f7e6',
                  border: `1px solid ${r.report_type === 'experiment' ? '#90c4e8' : '#a8d08d'}`,
                }}>
                  {r.report_type}
                </span>
              </td>
              <td style={{ ...cellStyle, fontFamily: 'monospace' }}>
                {r.experiment_id || '—'}
              </td>
              <td style={{ ...cellStyle, fontFamily: 'monospace', color: '#666' }}>
                {r.series_id || '—'}
              </td>
              <td style={{ ...cellStyle, color: '#555' }}>
                {r.generated_at ? r.generated_at.slice(0, 19).replace('T', ' ') : '—'}
              </td>
              <td style={cellStyle}>
                <button
                  style={{ ...btnStyle, marginRight: 4 }}
                  onClick={e => { e.stopPropagation(); onSelect && onSelect(r.report_id) }}
                >
                  View
                </button>
                <button
                  style={btnStyle}
                  onClick={e => handleDownloadJson(r.report_id, e)}
                >
                  ↓ JSON
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
