/**
 * Chat UI root — full-height chat interface for Local Corvus.
 */

import ChatWindow from './components/ChatWindow'

export default function App() {
  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      height: '100vh',
      maxWidth: 800,
      margin: '0 auto',
      padding: '16px',
      boxSizing: 'border-box',
    }}>
      <h1 style={{ fontSize: 16, marginBottom: 8, fontFamily: 'monospace', borderBottom: '1px solid #ccc', paddingBottom: 6 }}>
        Local Corvus — Chat
      </h1>
      <div style={{ flex: 1, minHeight: 0 }}>
        <ChatWindow />
      </div>
    </div>
  )
}
