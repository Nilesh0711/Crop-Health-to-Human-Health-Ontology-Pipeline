import { useEffect, useState } from 'react'
import { useData, Notice } from './useData.jsx'
import TermBrowser from './TermBrowser.jsx'
import MappingReview from './MappingReview.jsx'
import Reasoning from './Reasoning.jsx'
import Runs from './Runs.jsx'
import Entities from './Entities.jsx'
import Relations from './Relations.jsx'
import Appendix from './Appendix.jsx'

const views = [
  ['browse', 'Ontology'], ['entities', 'Entity registry'], ['relations', 'Relation registry'],
  ['review', 'Mapping review'], ['reasoning', 'Reasoning'], ['runs', 'Runs'], ['appendix', 'Appendix'],
]

export default function OntologyApp() {
  const [view, setView] = useState(() => {
    const initial = typeof window !== 'undefined' ? new URLSearchParams(window.location.search).get('view') : null
    return views.some(([key]) => key === initial) ? initial : 'browse'
  })
  const [selected, setSelected] = useState(() => typeof window !== 'undefined' ? new URLSearchParams(window.location.search).get('term') : null)
  const [revision, setRevision] = useState(0)
  const [utilityRevision, setUtilityRevision] = useState(0)
  const status = useData('/ontology/status', revision)
  const papers = useData('/pdfs', utilityRevision)
  const health = useData('/health', utilityRevision)
  function refresh() { setRevision(n => n + 1); setUtilityRevision(n => n + 1) }
  function select(iri) { setSelected(iri); setView('browse') }
  useEffect(() => {
    const url = new URL(window.location.href)
    if (selected) url.searchParams.set('term', selected); else url.searchParams.delete('term')
    if (view && view !== 'browse') url.searchParams.set('view', view); else url.searchParams.delete('view')
    window.history.replaceState(null, '', url)
  }, [selected, view])
  return <div className="ontology-app">
    <header><div><h1>Reasoning with Food Knowledge Graphs</h1><p>Soil, potato phosphorus, dietary phosphate &amp; human health</p></div></header>
    <nav aria-label="Application views">{views.map(([key, title]) =>
      <button key={key} className={view === key ? 'active' : ''} aria-current={view === key ? 'page' : undefined} onClick={() => setView(key)}>{title}</button>)}</nav>
    <Notice error={status.error} />
    {view === 'browse' && <TermBrowser selected={selected} onSelect={select} revision={revision} status={status.data} />}
    {view === 'entities' && <Entities revision={revision} />}
    {view === 'relations' && <Relations revision={revision} />}
    {view === 'review' && <MappingReview revision={revision} onRefresh={refresh} pdfs={papers.data || []} canReview={health.data?.capabilities?.execute === true} />}
    {view === 'reasoning' && <Reasoning revision={revision} />}
    {view === 'appendix' && <Appendix />}
    {view === 'runs' && <Runs pdfs={papers.data || []} health={health.data} onRefresh={refresh}
      loading={papers.loading} error={papers.error || health.error} />}
  </div>
}
