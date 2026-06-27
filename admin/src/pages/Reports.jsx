/**
 * Reports page — list, view, and generate experiment and series reports.
 *
 * Layout:
 *   ┌─────────────────────────────┐
 *   │  Filter + ReportsList       │
 *   ├─────────────────────────────┤
 *   │  Generate New… form         │
 *   ├─────────────────────────────┤
 *   │  ReportViewer (Markdown)    │
 *   │  ExperimentComparison table │
 *   └─────────────────────────────┘
 */

import { useState } from 'react'
import ReportsList from '../components/ReportsList'
import ReportViewer from '../components/ReportViewer'
import ExperimentComparison from '../components/ExperimentComparison'

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

const inputStyle = {
  fontFamily: 'monospace', fontSize: 12, padding: '5px 8px',
  border: '1px solid #ccc', borderRadius: 3, width: '100%', boxSizing: 'border-box',
}
const labelStyle = { fontSize: 11, color: '#555', display: 'block', marginBottom: 2 }
const btnStyle = {
  fontFamily: 'monospace', fontSize: 12, padding: '6px 14px',
  cursor: 'pointer', border: '1px solid #999', borderRadius: 3, background: '#f0f0f0',
}
const sectionStyle = { marginBottom: 16, paddingBottom: 16, borderBottom: '1px solid #eee' }

export default function Reports() {
  const [selectedId, setSelectedId] = useState(null)
  const [loadedReport, setLoadedReport] = useState(null)

  // Generate form state
  const [genType, setGenType] = useState('experiment')
  const [genExpId, setGenExpId] = useState('')
  const [genSeriesId, setGenSeriesId] = useState('')
  const [genStaticId, setGenStaticId] = useState('')
  const [genRandomId, setGenRandomId] = useState('')
  const [genLoading, setGenLoading] = useState(false)
  const [genResult, setGenResult] = useState(null)
  const [genError, setGenError] = useState(null)

  // Refresh key to force ReportsList to refetch after a new report is generated
  const [refreshKey, setRefreshKey] = useState(0)

  const handleSelect = async (reportId) => {
    setSelectedId(reportId)
    setLoadedReport(null)
    // Fetch JSON to check for control_shadow (for comparison table)
    try {
      const resp = await fetch(`${API_URL}/admin/reports/${reportId}`)
      if (resp.ok) {
        const data = await resp.json()
        setLoadedReport(data)
      }
    } catch (e) {
      // Non-fatal — comparison table just won't render
    }
  }

  const handleGenerate = async () => {
    if (!genExpId.trim()) {
      setGenError('experiment_id is required')
      return
    }
    if (genType === 'series' && !genSeriesId.trim()) {
      setGenError('series_id is required for a series report')
      return
    }

    setGenLoading(true)
    setGenError(null)
    setGenResult(null)

    try {
      const url = genType === 'series'
        ? `${API_URL}/admin/reports/generate-series`
        : `${API_URL}/admin/reports/generate-experiment`

      const body = genType === 'series'
        ? { experiment_id: genExpId.trim(), series_id: genSeriesId.trim() }
        : (() => {
            const payload = { experiment_id: genExpId.trim() }
            const ctrl = {}
            if (genStaticId.trim()) ctrl.static_weight = genStaticId.trim()
            if (genRandomId.trim()) ctrl.random_weight = genRandomId.trim()
            if (Object.keys(ctrl).length > 0) payload.control_experiment_ids = ctrl
            return payload
          })()

      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}))
        throw new Error(err.detail || `HTTP ${resp.status}`)
      }

      const result = await resp.json()
      setGenResult(result)
      setRefreshKey(k => k + 1)
    } catch (err) {
      setGenError(err.message)
    } finally {
      setGenLoading(false)
    }
  }

  return (
    <div>
      {/* Report list */}
      <div style={sectionStyle}>
        <ReportsList
          key={refreshKey}
          onSelect={handleSelect}
          selectedId={selectedId}
        />
      </div>

      {/* Generate new report form */}
      <div style={sectionStyle}>
        <div style={{ fontWeight: 'bold', fontSize: 13, marginBottom: 10 }}>
          Generate New Report
        </div>
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <div>
            <label style={labelStyle}>Report type</label>
            <select
              value={genType}
              onChange={e => setGenType(e.target.value)}
              style={{ ...inputStyle, width: 140 }}
            >
              <option value="experiment">Experiment</option>
              <option value="series">Series</option>
            </select>
          </div>

          <div style={{ minWidth: 200 }}>
            <label style={labelStyle}>experiment_id</label>
            <input
              value={genExpId}
              onChange={e => setGenExpId(e.target.value)}
              placeholder="e.g. learning-2026-07-01"
              style={inputStyle}
            />
          </div>

          {genType === 'series' && (
            <div style={{ minWidth: 180 }}>
              <label style={labelStyle}>series_id</label>
              <input
                value={genSeriesId}
                onChange={e => setGenSeriesId(e.target.value)}
                placeholder="e.g. series-1"
                style={inputStyle}
              />
            </div>
          )}

          {genType === 'experiment' && (
            <>
              <div style={{ minWidth: 200 }}>
                <label style={labelStyle}>static_weight experiment_id (optional)</label>
                <input
                  value={genStaticId}
                  onChange={e => setGenStaticId(e.target.value)}
                  placeholder="e.g. static-2026-07-10"
                  style={inputStyle}
                />
              </div>
              <div style={{ minWidth: 200 }}>
                <label style={labelStyle}>random_weight experiment_id (optional)</label>
                <input
                  value={genRandomId}
                  onChange={e => setGenRandomId(e.target.value)}
                  placeholder="e.g. random-2026-07-10"
                  style={inputStyle}
                />
              </div>
            </>
          )}

          <div>
            <button onClick={handleGenerate} style={btnStyle} disabled={genLoading}>
              {genLoading ? 'Generating…' : 'Generate'}
            </button>
          </div>
        </div>

        {genError && (
          <div style={{ color: '#c00', fontSize: 12, marginTop: 8 }}>
            Error: {genError}
          </div>
        )}
        {genResult && (
          <div style={{ color: '#2a7', fontSize: 12, marginTop: 8 }}>
            Generated: {genResult.report_id}
          </div>
        )}
      </div>

      {/* Report viewer */}
      {selectedId && (
        <div>
          <div style={{ fontWeight: 'bold', fontSize: 13, marginBottom: 8 }}>
            Report: {selectedId}
          </div>

          {/* Comparison table — auto-renders if control_shadow is present */}
          {loadedReport && <ExperimentComparison report={loadedReport} />}

          {/* Markdown viewer */}
          <div style={{
            border: '1px solid #ddd', borderRadius: 4,
            maxHeight: 600, overflow: 'auto', marginTop: 12,
          }}>
            <ReportViewer reportId={selectedId} />
          </div>
        </div>
      )}
    </div>
  )
}
