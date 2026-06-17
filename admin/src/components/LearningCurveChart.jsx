/**
 * LearningCurveChart — plots latency_ms over interaction index.
 *
 * X axis: interaction number (chronological order, oldest first).
 * Y axis: latency_ms.
 * Points are colored by feedback_signal: green (+1), red (-1), grey (none).
 *
 * This is the primary metric available from the run log without external
 * evaluation. Latency trends reflect model load, context growth, and tool
 * use patterns. A second series for edge weight average is overlaid if
 * weight_updates are present.
 */

import { useEffect, useState, useCallback } from 'react'
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  Legend, ResponsiveContainer, Scatter, ScatterChart, ZAxis,
} from 'recharts'
import { fetchRuns } from '../api'

export default function LearningCurveChart() {
  const [data, setData] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      // Fetch up to 500 most recent runs; reverse to chronological order
      const res = await fetchRuns({ limit: 500, offset: 0 })
      const runs = (res.runs || []).slice().reverse()
      const points = runs.map((r, i) => {
        const avgNewWeight = r.weight_updates?.length
          ? r.weight_updates.reduce((sum, u) => sum + (u.new_weight || 0), 0) / r.weight_updates.length
          : null
        return {
          index: i + 1,
          latency_ms: r.latency_ms ?? null,
          feedback: r.feedback_signal,
          avg_new_weight: avgNewWeight,
          run_id: r.run_id?.slice(0, 8),
        }
      })
      setData(points)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  function tooltipFormatter(value, name) {
    if (name === 'latency_ms') return [`${value} ms`, 'Latency']
    if (name === 'avg_new_weight') return [value?.toFixed(4), 'Avg edge weight after update']
    return [value, name]
  }

  function dotColor(entry) {
    if (entry.feedback === 1) return '#22c55e'
    if (entry.feedback === -1) return '#ef4444'
    return '#94a3b8'
  }

  if (loading) return <p>Loading…</p>
  if (error) return <p style={{ color: 'red' }}>Error: {error}</p>
  if (data.length === 0) return <p style={{ color: '#888' }}>No run data yet. Send some messages first.</p>

  return (
    <div>
      <div style={{ marginBottom: 8, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontSize: 12, color: '#555' }}>
          {data.length} interaction(s) &nbsp;|&nbsp;
          <span style={{ color: '#22c55e' }}>● +1 feedback</span> &nbsp;
          <span style={{ color: '#ef4444' }}>● -1 feedback</span> &nbsp;
          <span style={{ color: '#94a3b8' }}>● no feedback</span>
        </span>
        <button onClick={load} style={{ padding: '2px 8px', fontSize: 12 }}>Refresh</button>
      </div>

      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={data} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="index" label={{ value: 'Interaction #', position: 'insideBottom', offset: -2 }} />
          <YAxis yAxisId="left" label={{ value: 'Latency (ms)', angle: -90, position: 'insideLeft', offset: 10 }} />
          <YAxis yAxisId="right" orientation="right" domain={[0, 1]}
            label={{ value: 'Avg edge weight', angle: 90, position: 'insideRight', offset: 10 }} />
          <Tooltip formatter={tooltipFormatter} />
          <Legend verticalAlign="top" />
          <Line
            yAxisId="left"
            type="monotone"
            dataKey="latency_ms"
            stroke="#6366f1"
            dot={(props) => {
              const { cx, cy, payload } = props
              return <circle key={payload.index} cx={cx} cy={cy} r={4} fill={dotColor(payload)} stroke="none" />
            }}
            name="Latency (ms)"
          />
          <Line
            yAxisId="right"
            type="monotone"
            dataKey="avg_new_weight"
            stroke="#f59e0b"
            dot={false}
            strokeDasharray="4 2"
            name="Avg edge weight (post-update)"
            connectNulls={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
