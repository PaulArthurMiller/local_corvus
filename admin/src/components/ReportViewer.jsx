/**
 * ReportViewer — fetches and renders a report's Markdown content as HTML.
 *
 * Uses react-markdown with remark-gfm to render GFM tables.
 * Falls back to <pre> if react-markdown is not available.
 */

import { useState, useEffect } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

export default function ReportViewer({ reportId }) {
  const [markdown, setMarkdown] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!reportId) return
    setLoading(true)
    setError(null)
    setMarkdown(null)
    fetch(`${API_URL}/admin/reports/${reportId}/markdown`)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.text()
      })
      .then(text => setMarkdown(text))
      .catch(err => setError(err.message))
      .finally(() => setLoading(false))
  }, [reportId])

  if (!reportId) {
    return (
      <div style={{ color: '#888', fontStyle: 'italic', padding: 16, fontSize: 12 }}>
        Select a report from the list to view it here.
      </div>
    )
  }

  if (loading) {
    return <div style={{ padding: 16, fontSize: 12, color: '#555' }}>Loading report…</div>
  }

  if (error) {
    return (
      <div style={{ padding: 16, fontSize: 12, color: '#c00' }}>
        Error loading report: {error}
      </div>
    )
  }

  if (!markdown) return null

  return (
    <div style={{ padding: 8, fontSize: 13, lineHeight: 1.6, maxWidth: '100%', overflow: 'auto' }}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          table: ({ node, ...props }) => (
            <table
              style={{
                borderCollapse: 'collapse',
                width: '100%',
                fontSize: 12,
                marginBottom: 12,
              }}
              {...props}
            />
          ),
          th: ({ node, ...props }) => (
            <th
              style={{
                border: '1px solid #ccc',
                padding: '4px 8px',
                background: '#f5f5f5',
                textAlign: 'left',
              }}
              {...props}
            />
          ),
          td: ({ node, ...props }) => (
            <td
              style={{ border: '1px solid #ddd', padding: '4px 8px' }}
              {...props}
            />
          ),
          h1: ({ node, ...props }) => (
            <h1 style={{ fontSize: 18, borderBottom: '2px solid #333', paddingBottom: 4 }} {...props} />
          ),
          h2: ({ node, ...props }) => (
            <h2 style={{ fontSize: 15, borderBottom: '1px solid #ccc', paddingBottom: 2, marginTop: 20 }} {...props} />
          ),
          h3: ({ node, ...props }) => (
            <h3 style={{ fontSize: 13, marginTop: 16 }} {...props} />
          ),
          code: ({ node, inline, ...props }) =>
            inline
              ? <code style={{ background: '#f0f0f0', padding: '1px 4px', borderRadius: 3 }} {...props} />
              : <code style={{ display: 'block', background: '#f8f8f8', padding: 10, borderRadius: 4, overflow: 'auto' }} {...props} />,
          hr: () => <hr style={{ border: 'none', borderTop: '1px solid #ddd', margin: '16px 0' }} />,
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  )
}
