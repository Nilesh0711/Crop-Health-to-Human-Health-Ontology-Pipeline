import { createStaticHandler } from './staticApi.js'

export const API = '/api'

export function isStaticViewer() {
  return import.meta.env?.VITE_STATIC_VIEWER === '1'
}

export function basePath() {
  const base = import.meta.env?.BASE_URL || '/'
  return base.endsWith('/') ? base : `${base}/`
}

const staticHandler = createStaticHandler(async file => {
  const response = await fetch(`${basePath()}data/${file}`)
  if (!response.ok) {
    const error = new Error(`Static data missing (${response.status}): ${file}`)
    error.status = response.status
    throw error
  }
  return response.json()
})

export async function request(path, options) {
  if (isStaticViewer()) {
    return staticHandler(path, options)
  }
  const response = await fetch(`${API}${path}`, options)
  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    try { detail = (await response.json()).detail || detail } catch { /* response has no JSON body */ }
    const error = new Error(detail)
    error.status = response.status
    throw error
  }
  return response.json()
}
