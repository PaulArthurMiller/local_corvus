/**
 * ExperimentComparison — renders a side-by-side comparison table when an
 * experiment report contains a control_shadow.
 *
 * Shows: final probe pass rate, bridge-MU rank, edge delta magnitude, and
 * mean gold_mu_rank — learned condition vs. each control condition.
 */

const MEASURE_LABELS = {
  probe_pass_rate_delta: 'Final probe pass rate Δ',
  bridge_mu_rank_delta: 'Bridge-MU rank Δ (final session)',
  edge_weight_delta_magnitude: 'Mean edge weight |Δ| diff',
  mean_gold_mu_rank_delta: 'Mean gold_mu_rank Δ (final session)',
}

function StatusBadge({ status }) {
  const colors = { PASS: '#2a7', FAIL: '#c33', SKIP: '#888' }
  const color = colors[status] || '#555'
  return (
    <span style={{
      display: 'inline-block',
      padding: '1px 6px',
      borderRadius: 3,
      border: `1px solid ${color}`,
      color,
      fontSize: 11,
      fontWeight: 'bold',
    }}>
      {status}
    </span>
  )
}

function SanityChecks({ sanityChecks, controlName }) {
  if (!sanityChecks || Object.keys(sanityChecks).length === 0) return null
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontWeight: 'bold', fontSize: 12, marginBottom: 4 }}>
        Sanity Checks — {controlName}
      </div>
      <table style={{ borderCollapse: 'collapse', fontSize: 12 }}>
        <tbody>
          {Object.entries(sanityChecks).map(([check, result]) => (
            <tr key={check}>
              <td style={{ padding: '3px 8px', color: '#555' }}>{check}</td>
              <td style={{ padding: '3px 8px' }}>
                <StatusBadge status={result.status} />
              </td>
              <td style={{ padding: '3px 8px', color: '#666' }}>
                {result.violation_count !== undefined
                  ? `${result.violation_count} violation(s)`
                  : result.reason || ''}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function ExperimentComparison({ report }) {
  const controlShadow = report?.control_shadow
  if (!controlShadow || Object.keys(controlShadow).length === 0) return null

  const controlNames = Object.keys(controlShadow)
  const headStyle = {
    padding: '6px 10px', textAlign: 'left',
    background: '#f5f5f5', fontWeight: 'bold', fontSize: 12,
    borderBottom: '2px solid #ccc',
  }
  const cellStyle = { padding: '6px 10px', fontSize: 12, borderBottom: '1px solid #eee' }

  return (
    <div style={{ marginTop: 20 }}>
      <h3 style={{ fontSize: 14, marginBottom: 12, borderBottom: '1px solid #ddd', paddingBottom: 4 }}>
        Condition Comparison (§6.2)
      </h3>

      {/* Sanity checks per control */}
      {controlNames.map(name => (
        <SanityChecks
          key={name}
          sanityChecks={controlShadow[name]?.sanity_checks}
          controlName={`${name} (${controlShadow[name]?.condition_label || '—'})`}
        />
      ))}

      {/* Comparative delta table */}
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12, marginTop: 8 }}>
        <thead>
          <tr>
            <th style={headStyle}>Measure</th>
            <th style={{ ...headStyle, color: '#2a5' }}>Learned (baseline)</th>
            {controlNames.map(name => (
              <th key={name} style={{ ...headStyle, color: '#555' }}>
                {name}<br />
                <span style={{ fontWeight: 'normal', color: '#777', fontSize: 11 }}>
                  {controlShadow[name]?.condition_label || '—'}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {Object.entries(MEASURE_LABELS).map(([key, label]) => (
            <tr key={key}>
              <td style={cellStyle}>{label}</td>
              <td style={{ ...cellStyle, color: '#555', fontStyle: 'italic' }}>—</td>
              {controlNames.map(name => {
                const delta = controlShadow[name]?.comparative_deltas?.[key]
                const formatted = delta !== null && delta !== undefined
                  ? (delta >= 0 ? '+' : '') + delta.toFixed(4)
                  : '—'
                const color = delta == null ? '#999' : delta > 0 ? '#2a7' : '#c33'
                return (
                  <td key={name} style={{ ...cellStyle, color, fontWeight: delta != null ? 'bold' : 'normal' }}>
                    {formatted}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>

      <p style={{ fontSize: 11, color: '#888', marginTop: 8, fontStyle: 'italic' }}>
        Deltas are computed as learned − control. Positive values indicate learned condition is higher.
        Note: interpretation depends on the measure (lower rank is better for gold_mu_rank).
      </p>
    </div>
  )
}
