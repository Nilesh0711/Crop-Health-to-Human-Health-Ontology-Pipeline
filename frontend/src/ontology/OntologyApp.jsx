import { useEffect, useState } from 'react'
import { useData, Notice } from './useData.jsx'
import TermBrowser from './TermBrowser.jsx'
import MappingReview from './MappingReview.jsx'
import Reasoning from './Reasoning.jsx'
import Runs from './Runs.jsx'
import Entities from './Entities.jsx'
import Appendix from './Appendix.jsx'

export default function OntologyApp() {
  const [view, setView] = useState(() => typeof window !== 'undefined' ? (new URLSearchParams(window.location.search).get('view') || 'browse') : 'browse')
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
    <header><div><h1>Reasoning with Food Knowledge Graphs</h1><p>Potato · soil nutrients and crop nutrition</p></div>
      <span className="muted">{health.data?.pipeline?.version || 'Ontology browser'}</span></header>
    <nav aria-label="Application views">{[['browse', 'Ontology'], ['entities', 'Entity registry'], ['review', 'Mapping review'], ['reasoning', 'Reasoning'], ['runs', 'Runs'], ['appendix', 'Appendix']].map(([key, title]) =>
      <button key={key} className={view === key ? 'active' : ''} aria-current={view === key ? 'page' : undefined} onClick={() => setView(key)}>{title}</button>)}</nav>
    <Notice error={status.error} />
    {status.data?.stage_status?.ontology_grounding === 'paused' && <p className="ontology-warning">Ontology grounding is paused. New runs stop after extraction; saved mappings are historical results.</p>}
    {status.data?.stage_status?.ontology_grounding === 'manual' && <p className="muted">Grounding is manual: extract first, then use Run grounded claims in Runs when ready. Pathway assembly and scientific validation remain paused.</p>}
    {view === 'browse' && <TermBrowser selected={selected} onSelect={select} revision={revision} status={status.data} />}
    {view === 'entities' && <Entities revision={revision} />}
    {view === 'review' && <MappingReview revision={revision} onRefresh={refresh} pdfs={papers.data || []} canReview={health.data?.capabilities?.execute === true} />}
    {view === 'reasoning' && <Reasoning revision={revision} />}
    {view === 'appendix' && <Appendix />}
    {view === 'runs' && <Runs pdfs={papers.data || []} health={health.data} onRefresh={refresh}
      loading={papers.loading} error={papers.error || health.error} />}
  </div>
}
