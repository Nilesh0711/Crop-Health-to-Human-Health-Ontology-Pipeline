import { useEffect, useMemo, useRef, useState } from 'react'
import { useData, encoded, Link, Notice, Pagination, label } from './useData.jsx'

const kinds = [['class', 'Classes'], ['object_property', 'Object properties'], ['data_property', 'Data properties']]

const establishedOntologies = [
  {
    name: 'Plant Ontology',
    prefix: 'PO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/PO',
    owlUrl: 'http://purl.obolibrary.org/obo/po.owl',
    scope: 'Plant anatomical structures, organs, vascular tissues, and morphological developmental stages.',
    examples: 'root, potato tuber, stolon, leaf, shoot system, lateral root, xylem, phloem'
  },
  {
    name: 'Plant Trait Ontology',
    prefix: 'TO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/TO',
    owlUrl: 'http://purl.obolibrary.org/obo/to.owl',
    scope: 'Plant phenotypic, nutrient sensitivity/deficiency, uptake, biochemical, root architecture, and yield traits.',
    examples: 'nitrogen sensitivity / deficiency, phosphorus response / deficiency, potassium sensitivity / deficiency, phosphorus uptake, nitrate uptake, root length to shoot length ratio, plant height, plant fresh weight, root system phosphorus content, shoot axis tuber yield'
  },
  {
    name: 'Gene Ontology',
    prefix: 'GO',
    standard: 'Gene Ontology Consortium',
    ontobeeUrl: 'https://ontobee.org/ontology/GO',
    owlUrl: 'http://purl.obolibrary.org/obo/go.owl',
    scope: 'Biological processes, transmembrane transporter activities, cellular starvation responses, carbon fixation, symbiosis, and biosynthetic pathways.',
    examples: 'phosphate transmembrane transporter activity, nitrate transmembrane transporter activity, ammonium channel activity, gene expression, carbon fixation, biological process involved in symbiotic interaction, indoleacetic acid biosynthetic process, cellular response to phosphate starvation'
  },
  {
    name: 'Sequence Ontology',
    prefix: 'SO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/SO',
    owlUrl: 'http://purl.obolibrary.org/obo/so.owl',
    scope: 'Biological sequences, sequence features, and generic gene concepts.',
    examples: 'gene'
  },
  {
    name: 'Chemical Entities of Biological Interest',
    prefix: 'CHEBI',
    standard: 'EMBL-EBI / OBO Foundry',
    ontobeeUrl: 'https://ontobee.org/ontology/CHEBI',
    owlUrl: 'http://purl.obolibrary.org/obo/chebi.owl',
    scope: 'Mineral nutrients, chemical compounds, ions, fertilizers, organophosphorus compounds, proteins, and primary/secondary plant metabolites.',
    examples: 'phosphorus atom, phosphate(3-), organophosphorus compound, protein, nitrate, ammonium, potassium atom, starch, sucrose, fertilizer'
  },
  {
    name: 'Environment Ontology',
    prefix: 'ENVO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/ENVO',
    owlUrl: 'http://purl.obolibrary.org/obo/envo.owl',
    scope: 'Environmental biomes, soil types, rhizosphere environments, and soil chemical properties.',
    examples: 'soil, rhizosphere, fertilized soil, loam, sandy loam, acidity of soil'
  },
  {
    name: 'Plant Experimental Conditions Ontology',
    prefix: 'PECO',
    standard: 'Gramene / OBO Foundry',
    ontobeeUrl: 'https://ontobee.org/ontology/PECO',
    owlUrl: 'http://purl.obolibrary.org/obo/peco.owl',
    scope: 'Plant experimental treatments, nutrient exposures, fertilizer regimes, and growth media.',
    examples: 'phosphorus nutrient exposure, limited phosphate exposure, nitrogen exposure, limited nitrogen exposure, NPK fertilizer exposure, plant growth medium exposure'
  },
  {
    name: 'Phenotype and Trait Ontology',
    prefix: 'PATO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/PATO',
    owlUrl: 'http://purl.obolibrary.org/obo/pato.owl',
    scope: 'Qualitative, comparative, and phenotypic dimensional qualities.',
    examples: 'concentration of, length, area, volume, mass, ratio'
  },
  {
    name: 'NCBI Organismal Taxonomy',
    prefix: 'NCBITaxon',
    standard: 'NCBI Taxonomy / OBO',
    ontobeeUrl: 'https://ontobee.org/ontology/NCBITaxon',
    owlUrl: 'http://purl.obolibrary.org/obo/ncbitaxon.owl',
    scope: 'Taxonomic hierarchy of crop plants, rhizosphere bacteria, and fungi.',
    examples: 'Solanum tuberosum, Bacillus subtilis, Bacteria, Fungi'
  },
  {
    name: 'Relation Ontology & Basic Formal Ontology',
    prefix: 'RO / BFO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/RO',
    owlUrl: 'http://purl.obolibrary.org/obo/ro.owl',
    scope: 'Causal, regulatory, associative, production, mereological, bearer-quality, process-participation, and transport object properties.',
    examples: 'part of, has part, has quality, characteristic of, located in, participates in, has participant, transports, has input, has output, causally related to, causally upstream of (positive/negative effect), regulates (positively/negatively), produces, correlated with'
  }
]

function TreeNode({ term, kind, selected, onSelect, ancestors = [], expandedSet }) {
  const isAncestorOfSelected = Boolean(expandedSet && expandedSet.has(term.iri))
  const [userOpen, setUserOpen] = useState(null)
  const [offset, setOffset] = useState(0)
  const repeated = ancestors.includes(term.iri)
  const rowRef = useRef(null)

  // Auto-expand if this node is an ancestor of the currently selected term
  useEffect(() => {
    if (isAncestorOfSelected) {
      setUserOpen(true)
    }
  }, [isAncestorOfSelected])

  // Smooth scroll into view when selected
  useEffect(() => {
    if (selected === term.iri && rowRef.current) {
      rowRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  }, [selected, term.iri])

  const open = userOpen !== null ? userOpen : isAncestorOfSelected
  const children = useData(open && !repeated ? `/ontology/hierarchy?kind=${kind}&parent=${encoded(term.iri)}&limit=40&offset=${offset}` : null)

  return (
    <li>
      <div ref={rowRef} className={`ontology-tree-row ${selected === term.iri ? 'selected' : ''}`}>
        <button
          type="button"
          className="tree-expander"
          aria-label={`${open ? 'Collapse' : 'Expand'} ${term.label}`}
          aria-expanded={open}
          disabled={!term.has_children || repeated}
          onClick={() => setUserOpen(!open)}
        >
          {term.has_children && !repeated ? (open ? '−' : '+') : '·'}
        </button>
        <button type="button" className="term-link" onClick={() => onSelect(term.iri)}>
          {term.label}
        </button>
        {selected === term.iri && <span className="tree-selected-badge">selected</span>}
      </div>
      {open && !repeated && (
        <>
          <Notice {...children} />
          <ul>
            {children.data?.terms.map(child => (
              <TreeNode
                key={child.iri}
                term={child}
                kind={kind}
                selected={selected}
                onSelect={onSelect}
                ancestors={[...ancestors, term.iri]}
                expandedSet={expandedSet}
              />
            ))}
          </ul>
          {children.data && <Pagination {...children.data} onChange={setOffset} />}
        </>
      )}
    </li>
  )
}

function renderOntobeeNode(steps, index, currentTerm, children, onSelect) {
  if (index >= steps.length - 1) {
    const hasChildren = Boolean(children && children.length > 0)
    return (
      <li key={currentTerm.iri} className="ontobee-current">
        <span className="ontobee-current-label">
          <strong>{currentTerm.label}</strong>{' '}
          {currentTerm.id && <span className="muted">[{currentTerm.id}]</span>}
        </span>
        {hasChildren && (
          <ul>
            {children.map(child => (
              <li key={child.iri}>
                <button
                  type="button"
                  className="term-link"
                  onClick={() => onSelect(child.iri)}
                >
                  {child.label}
                </button>
                {child.id && <span className="muted"> [{child.id}]</span>}
              </li>
            ))}
          </ul>
        )}
      </li>
    )
  }

  const step = steps[index]
  return (
    <li key={step.iri}>
      <button
        type="button"
        className="term-link"
        onClick={() => onSelect(step.iri)}
      >
        {step.label}
      </button>
      {step.id && <span className="muted"> [{step.id}]</span>}
      <ul>
        {renderOntobeeNode(steps, index + 1, currentTerm, children, onSelect)}
      </ul>
    </li>
  )
}

function OntobeeHierarchy({ term, onSelect }) {
  const paths = term.lineage_paths && term.lineage_paths.length > 0
    ? term.lineage_paths
    : [[{ iri: term.iri, label: term.label, id: term.id }]]

  return (
    <section className="ontobee-hierarchy" aria-label="Class hierarchy">
      <h3>{term.kind === 'class' ? 'Class Hierarchy' : 'Property Hierarchy'}</h3>
      {paths.map((path, idx) => (
        <div key={idx} className="ontobee-path-block">
          {paths.length > 1 && (
            <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>
              Lineage {idx + 1}:
            </div>
          )}
          <ul className="ontobee-tree">
            {renderOntobeeNode(path, 0, term, term.children, onSelect)}
          </ul>
        </div>
      ))}
    </section>
  )
}

function TermDetails({ iri, onSelect, revision }) {
  const term = useData(iri ? `/ontology/term?iri=${encoded(iri)}` : null, revision)
  const evidence = useData(iri ? `/ontology/assertions?iri=${encoded(iri)}&limit=10` : null, revision)

  if (!iri) {
    return (
      <article className="ontology-introduction">
        <h2>Browse the potato nutrition ontology</h2>
        <p>Search for a concept or expand the hierarchy. Select a term to read its definition and the extracted claims linked to it.</p>
        <p className="muted">The hierarchy organizes concepts. Scientific relationships and evidence appear in tables. Source fidelity and external validation are paused.</p>
      </article>
    )
  }

  if (!term.data) return <article><Notice {...term} /></article>
  const t = term.data
  const ontoPrefix = t.id && t.id.includes(':') && t.iri?.startsWith('http://purl.obolibrary.org/obo/')
    ? t.id.split(':')[0]
    : null
  const ontobeeTermUrl = ontoPrefix
    ? `https://ontobee.org/ontology/${ontoPrefix === 'BFO' ? 'RO' : ontoPrefix}?iri=${encodeURIComponent(t.iri)}`
    : null

  return (
    <article className="ontology-term" aria-label="Selected ontology term">
      <h2>{t.label}</h2>
      <p className="term-identifier">
        <Link iri={t.iri}>{t.id}</Link>{' '}
        {ontobeeTermUrl && (
          <>
            · <Link iri={ontobeeTermUrl}>Ontobee</Link>{' '}
          </>
        )}
        <span className="muted">{t.origin === 'project_schema' ? 'Application metadata' : t.selectable ? 'Selectable established ontology term' : 'Supporting ancestor term'}</span>
      </p>

      {/* Ontobee-style Class Hierarchy */}
      <OntobeeHierarchy term={t} onSelect={onSelect} />

      <dl className="term-fields">
        <dt>Definition</dt>
        <dd>{t.definition || 'No textual definition supplied in this reference fragment.'}</dd>
        {!!t.exact_synonyms?.length && (
          <>
            <dt>Exact synonyms</dt>
            <dd>{t.exact_synonyms.join('; ')}</dd>
          </>
        )}
        {!!t.related_synonyms?.length && (
          <>
            <dt>Related synonyms</dt>
            <dd>{t.related_synonyms.join('; ')}</dd>
          </>
        )}
        {!t.exact_synonyms?.length && !t.related_synonyms?.length && !!t.synonyms?.length && (
          <>
            <dt>Synonyms</dt>
            <dd>{t.synonyms.join('; ')}</dd>
          </>
        )}
        {t.kind !== 'class' && (
          <>
            <dt>Domain</dt>
            <dd>
              {t.domain.map(i => (
                <button key={i} type="button" className="term-link" onClick={() => onSelect(i)}>
                  {label(i)}
                </button>
              ))}
              {!t.domain.length && 'Not declared in this fragment'}
            </dd>
            <dt>Range / datatype</dt>
            <dd>
              {t.range.map(i => <span key={i}>{label(i)} </span>)}
              {!t.range.length && 'Not declared in this fragment'}
            </dd>
          </>
        )}
      </dl>

      <section style={{ marginTop: 24, borderTop: '1px solid #d8dde1', paddingTop: 14 }}>
        <h3>Reference and identifiers</h3>
        <p className="break-word">{t.iri}</p>
        <p className="muted" style={{ fontSize: 13 }}>
          {t.origin === 'established_ontology'
            ? 'Imported ontology term. Local files are frozen and checked by content hash.'
            : t.origin === 'project_schema'
            ? 'Application bookkeeping metadata; not a scientific grounding class or relation.'
            : 'Locally imported reference module. Consult its recorded source and selection rationale.'}
        </p>
        <ul>
          {t.sources.map(source => (
            <li key={source}><Link iri={source} /></li>
          ))}
        </ul>
        <small className="break-word muted">Reference signature: {t.catalog_signature}</small>
      </section>

      <details style={{ marginTop: 20, borderTop: '1px solid #d8dde1', paddingTop: 14 }}>
        <summary style={{ cursor: 'pointer', fontWeight: 600, fontSize: 14 }}>
          Linked assertions and source evidence ({t.assertion_count})
        </summary>
        <div style={{ marginTop: 10 }}>
          <Notice {...evidence} />
          {evidence.data?.stale && <p className="ontology-warning">Saved mappings need refresh.</p>}
          {evidence.data?.assertions.map(row => (
            <div className="compact-evidence" key={`${row.document_key}:${row.candidate_id}`}>
              <p><strong>{row.original_claim.subject} → {row.original_claim.relation} → {row.original_claim.object}</strong></p>
              <p className="muted">
                {row.document_key} · page {row.original_claim.source_page ?? 'not recorded'} · {row.scope.replaceAll('_', ' ')}
              </p>
              <p style={{ margin: '4px 0', fontSize: 13, fontStyle: 'italic' }}>&ldquo;{row.original_claim.source_evidence_span}&rdquo;</p>
              {Object.keys(row.original_claim.qualifiers || {}).length > 0 && (
                <dl>
                  {Object.entries(row.original_claim.qualifiers).map(([key, value]) => (
                    <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{String(value)}</dd></div>
                  ))}
                </dl>
              )}
            </div>
          ))}
          {evidence.data && !evidence.data.total && <p className="muted">No extracted assertions currently link to this term.</p>}
          {evidence.data?.total > 10 && <p className="muted">Showing the first ten. Use Mapping review to browse all assertions.</p>}
        </div>
      </details>
    </article>
  )
}

export default function TermBrowser({ selected, onSelect, revision, status: externalStatus }) {
  const localStatus = useData(!externalStatus ? '/ontology/status' : null, revision)
  const statusData = externalStatus || localStatus.data
  const [kind, setKind] = useState('class')
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const [onlyApplication, setOnlyApplication] = useState(true)

  // Fetch selected term details to extract ancestors for auto-expanding hierarchy
  const selectedTerm = useData(selected ? `/ontology/term?iri=${encoded(selected)}` : null, revision)

  // Sync active kind tab if selected term is an object or data property
  useEffect(() => {
    if (selectedTerm.data?.kind && selectedTerm.data.kind !== kind) {
      setKind(selectedTerm.data.kind)
    }
  }, [selectedTerm.data?.kind])

  // Build the set of IRIs that must be expanded to reveal the selected term
  const expandedSet = useMemo(() => {
    if (!selectedTerm.data?.ancestors) return new Set()
    return new Set(selectedTerm.data.ancestors)
  }, [selectedTerm.data?.ancestors])

  useEffect(() => {
    const timer = setTimeout(() => { setSearch(query); setOffset(0) }, 250)
    return () => clearTimeout(timer)
  }, [query])

  const tree = useData(!search ? `/ontology/hierarchy?kind=${kind}&offset=${offset}&limit=40` : null, revision)
  const found = useData(search ? `/ontology/terms?kind=${kind}&q=${encoded(search)}&selectable=${onlyApplication}&offset=${offset}&limit=40` : null, revision)
  const data = search ? found : tree

  return (
    <div className="ontology-view-container">
      {/* Top Table: Established Ontologies (collapsible dropdown, hidden by default) */}
      <section className="ontology-established-overview" style={{ marginBottom: 16 }}>
        <details>
          <summary style={{ cursor: 'pointer', fontWeight: 600, fontSize: 14 }}>
            Established Ontologies in Pipeline ({establishedOntologies.length})
          </summary>
          <div style={{ marginTop: 8 }}>
            <p className="muted" style={{ margin: '3px 0 8px', fontSize: 13 }}>
              All selectable classes are imported from established OBO Foundry ontologies as content-addressed RDF/OWL fragments from Ontobee with bounded supporting ancestors.
            </p>
            <div className="ontology-table-scroll">
              <table className="established-ontologies-table">
                <thead>
                  <tr>
                    <th style={{ width: '20%' }}>Ontology</th>
                    <th style={{ width: '10%' }}>Namespace</th>
                    <th style={{ width: '18%' }}>OWL &amp; Ontobee Links</th>
                    <th style={{ width: '52%' }}>Domain Scope &amp; Verified Examples</th>
                  </tr>
                </thead>
                <tbody>
                  {establishedOntologies.map(ont => (
                    <tr key={ont.prefix}>
                      <td>
                        <strong>{ont.name}</strong>
                        <span className="muted" style={{ display: 'block', fontSize: 11 }}>{ont.standard}</span>
                      </td>
                      <td><code>{ont.prefix}</code></td>
                      <td style={{ fontSize: 12 }}>
                        <div><Link iri={ont.ontobeeUrl}>Ontobee ({ont.prefix})</Link></div>
                        <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
                          <Link iri={ont.owlUrl}>{ont.owlUrl.split('/').pop()}</Link>
                        </div>
                      </td>
                      <td>
                        <div style={{ fontSize: 12 }}>{ont.scope}</div>
                        <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
                          <em>Examples:</em> {ont.examples}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </details>
      </section>

      {/* Main Two-Column Ontology Browser */}
      <div className="ontology-browser">
        <aside aria-label="Ontology term navigation">
          <div className="ontology-kinds">
            {kinds.map(([key, name]) => {
              const count = statusData?.term_counts?.[key]
              return (
                <button
                  key={key}
                  type="button"
                  className={kind === key ? 'active' : ''}
                  onClick={() => { setKind(key); setOffset(0) }}
                >
                  {count != null ? `${name} (${count})` : name}
                </button>
              )
            })}
          </div>

          <label className="search-label">
            Search terms
            <input
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="Name, synonym or identifier"
            />
          </label>

          {search && (
            <label className="inline-check" style={{ marginTop: 6 }}>
              <input
                type="checkbox"
                checked={onlyApplication}
                onChange={e => { setOnlyApplication(e.target.checked); setOffset(0) }}
              />
              Application terms only
            </label>
          )}

          <Notice {...data} />

          <ul className="ontology-tree">
            {data.data?.terms.map(term =>
              search ? (
                <li key={term.iri}>
                  <button type="button" className="term-link" onClick={() => onSelect(term.iri)}>
                    {term.label}
                  </button>
                  <small className="muted"> {term.id}</small>
                </li>
              ) : (
                <TreeNode
                  key={term.iri}
                  term={term}
                  kind={kind}
                  selected={selected}
                  onSelect={onSelect}
                  expandedSet={expandedSet}
                />
              )
            )}
          </ul>

          {data.data && !data.data.total && <p className="muted">No matching terms in this reference.</p>}
          {data.data && <Pagination {...data.data} onChange={setOffset} />}
        </aside>

        <TermDetails iri={selected} onSelect={onSelect} revision={revision} />
      </div>
    </div>
  )
}
