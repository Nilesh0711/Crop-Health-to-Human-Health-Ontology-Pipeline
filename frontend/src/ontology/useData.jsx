import { useEffect, useState } from 'react'
import { basePath, isStaticViewer, request } from '../lib/api.js'

export function useData(path, revision = 0) {
  const [state, setState] = useState({ path, data: null, error: '', loading: !!path })
  useEffect(() => {
    if (!path) { setState({ path, data: null, error: '', loading: false }); return }
    const controller = new AbortController()
    // A refresh must not temporarily remove the selected paper and its monitor.
    // A different resource, however, must never display the previous resource.
    setState(previous => ({ path, data: previous.path === path ? previous.data : null, error: '', loading: true }))
    request(path, { signal: controller.signal }).then(data => {
      if (!controller.signal.aborted) setState({ path, data, error: '', loading: false })
    }).catch(error => {
      if (!controller.signal.aborted) setState(previous => ({ path, data: previous.path === path ? previous.data : null, error: error.message, loading: false }))
    })
    return () => controller.abort()
  }, [path, revision])
  return state.path === path ? state : { data: null, error: '', loading: !!path }
}

export const encoded = value => encodeURIComponent(value)
export const label = value => String(value || '').split('/').pop().replaceAll('_', ':')
export function getTermUrl(iri) {
  if (!iri) return null
  return `${basePath()}?view=browse&term=${encodeURIComponent(iri)}`
}
export function getPdfUrl(pdf, page) {
  if (!pdf || pdf.pdf_available === false) return null
  const hash = page ? `#page=${Number(page) || 1}` : ''
  return isStaticViewer()
    ? `${basePath()}data/pdfs/${encoded(pdf.id)}/source.pdf${hash}`
    : `/api/pdfs/${encoded(pdf.id)}/file${hash}`
}
export function getOntologyUrl(iri) {
  if (!iri) return null
  if (iri.includes('/obo/')) {
    const id = iri.split('/obo/')[1]
    const prefix = id.split('_')[0]
    return `https://ontobee.org/ontology/${prefix}?iri=${encodeURIComponent(iri)}`
  }
  if (iri.startsWith('http://') || iri.startsWith('https://')) {
    return iri
  }
  return null
}
export function Link({ iri, children }) {
  return /^https?:\/\//.test(iri || '')
    ? <a href={iri} target="_blank" rel="noreferrer">{children || iri}</a>
    : <span>{children || iri}</span>
}
export function Notice({ error, loading }) {
  return error ? <p role="alert" className="ontology-error">{error}</p> : loading ? <p role="status" className="muted">Loading…</p> : null
}
export function Pagination({ offset, total, limit, onChange }) {
  if (total <= limit) return null
  return <div className="ontology-pagination"><button disabled={!offset} onClick={() => onChange(Math.max(0, offset - limit))}>Previous</button>
    <span>{offset + 1}–{Math.min(total, offset + limit)} of {total}</span>
    <button disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>Next</button></div>
}
