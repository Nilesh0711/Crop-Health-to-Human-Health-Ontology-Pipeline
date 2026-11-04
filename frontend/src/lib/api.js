export const API = '/api'

export async function request(path, options) {
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
