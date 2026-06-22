/**
 * Admin UI root — tab-based navigation between Run Log, Learning Curve, and Memory Inspector.
 */

import { useState } from 'react'
import RunLogTable from './components/RunLogTable'
import LearningCurveChart from './components/LearningCurveChart'
import MemoryInspector from './components/MemoryInspector'
import ProbeResults from './components/ProbeResults'

const TABS = [
  { id: 'runs', label: 'Run Log' },
  { id: 'curve', label: 'Learning Curve' },
  { id: 'graph', label: 'Memory Inspector' },
  { id: 'probes', label: 'Probe Results' },
]

export default function App() {
  const [activeTab, setActiveTab] = useState('runs')

  return (
    <div style={{ fontFamily: 'monospace', maxWidth: 1200, margin: '0 auto', padding: '16px' }}>
      <h1 style={{ fontSize: 18, marginBottom: 4, borderBottom: '2px solid #333', paddingBottom: 6 }}>
        Local Corvus — Admin
      </h1>

      <div style={{ display: 'flex', gap: 2, marginBottom: 16 }}>
        {TABS.map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            style={{
              padding: '6px 16px',
              fontFamily: 'monospace',
              fontSize: 13,
              cursor: 'pointer',
              border: '1px solid #999',
              borderBottom: activeTab === tab.id ? 'none' : '1px solid #999',
              background: activeTab === tab.id ? '#fff' : '#f0f0f0',
              fontWeight: activeTab === tab.id ? 'bold' : 'normal',
              borderRadius: '4px 4px 0 0',
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div style={{ border: '1px solid #999', padding: 16, minHeight: 400 }}>
        {activeTab === 'runs' && <RunLogTable />}
        {activeTab === 'curve' && <LearningCurveChart />}
        {activeTab === 'graph' && <MemoryInspector />}
        {activeTab === 'probes' && <ProbeResults />}
      </div>
    </div>
  )
}
