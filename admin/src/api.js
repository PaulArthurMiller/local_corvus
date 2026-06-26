/**
 * API client for the Local Corvus backend.
 *
 * All fetch calls go through these functions so the base URL is in one place.
 * The backend URL is set via VITE_API_URL env var; falls back to localhost:8000.
 */

const BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

async function get(path, params = {}) {
  const url = new URL(BASE_URL + path)
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null) url.searchParams.set(k, v)
  })
  const res = await fetch(url.toString())
  if (!res.ok) throw new Error(`GET ${path} → ${res.status} ${res.statusText}`)
  return res.json()
}

export async function fetchRuns({ seriesId, limit = 100, offset = 0 } = {}) {
  return get('/admin/runs', { series_id: seriesId, limit, offset })
}

export async function fetchGraph() {
  return get('/admin/graph')
}

export async function fetchConfig() {
  return get('/admin/config')
}
