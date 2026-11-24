import { useEffect, useState } from 'react'
import { closedPredicateList } from './appendixExamples.js'
import { useData, encoded, Link, Notice, Pagination, label } from './useData.jsx'

const kinds = [['class', 'Classes'], ['object_property', 'Object properties']]
const extractionProperties = new Map(closedPredicateList.map(property => [property.iri, property]))

const establishedOntologies = [
  {
    name: 'Plant Ontology',
    prefix: 'PO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/PO',
    owlUrl: 'http://purl.obolibrary.org/obo/po.owl',
    scope: 'Plant anatomical structures, organs, vascular tissues, and morphological developmental stages.',
    examples: 'seed'
  },
  {
    name: 'Plant Trait Ontology',
    prefix: 'TO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/TO',
    owlUrl: 'http://purl.obolibrary.org/obo/to.owl',
    scope: 'Plant phenotypic, nutrient uptake, root architecture, phosphorus content, and yield traits.',
    examples: 'phosphorus uptake, root morphology trait, phosphorus content, root system morphology trait, biomass yield'
  },
  {
    name: 'Gene Ontology',
    prefix: 'GO',
    standard: 'Gene Ontology Consortium',
    ontobeeUrl: 'https://ontobee.org/ontology/GO',
    owlUrl: 'http://purl.obolibrary.org/obo/go.owl',
    scope: 'Biological processes, phosphate ion transport, organ development, and metabolic pathways.',
    examples: 'growth, renal phosphate excretion, root system development, phosphorus utilization, phosphorus metabolic process'
  },
  {
    name: 'Chemical Entities of Biological Interest',
    prefix: 'CHEBI',
    standard: 'EMBL-EBI / OBO Foundry',
    ontobeeUrl: 'https://ontobee.org/ontology/CHEBI',
    owlUrl: 'http://purl.obolibrary.org/obo/chebi.owl',
    scope: 'Phosphate ions, organophosphorus compounds, phytate, and biochemical molecules.',
    examples: 'myo-inositol hexakisphosphate, inorganic phosphate, phosphate, hormone, calcium phosphate'
  },
  {
    name: 'Environment Ontology',
    prefix: 'ENVO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/ENVO',
    owlUrl: 'http://purl.obolibrary.org/obo/envo.owl',
    scope: 'Soil phosphorus concentration and environmental process classes.',
    examples: 'concentration of phosphorus molecular entity in soil, erosion, material extraction process'
  },
  {
    name: 'Plant Experimental Conditions Ontology',
    prefix: 'PECO',
    standard: 'Gramene / OBO Foundry',
    ontobeeUrl: 'https://ontobee.org/ontology/PECO',
    owlUrl: 'http://purl.obolibrary.org/obo/peco.owl',
    scope: 'Plant experimental treatments, nutrient exposures, phosphate limitation, and microbial inoculation.',
    examples: 'bacterial plant exposure, phosphorus nutrient exposure, phosphate exposure, limited phosphate exposure, biotic plant exposure'
  },
  {
    name: 'Phenotype and Trait Ontology',
    prefix: 'PATO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/PATO',
    owlUrl: 'http://purl.obolibrary.org/obo/pato.owl',
    scope: 'Qualitative and dimensional attributes such as efficiency, volume change, and accumulation.',
    examples: 'efficiency, viscosity, damage, decreased efficiency, accumulation'
  },
  {
    name: 'NCBI Organismal Taxonomy',
    prefix: 'NCBITaxon',
    standard: 'NCBI Taxonomy / OBO',
    ontobeeUrl: 'https://ontobee.org/ontology/NCBITaxon',
    owlUrl: 'http://purl.obolibrary.org/obo/ncbitaxon.owl',
    scope: 'Taxonomic hierarchy of crop plants and rhizosphere bacteria.',
    examples: 'Bacteria, Viridiplantae'
  },
  {
    name: 'Food Ontology',
    prefix: 'FOODON',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/FOODON',
    owlUrl: 'http://purl.obolibrary.org/obo/foodon.owl',
    scope: 'Food products, potato tubers, culinary preparation methods, and phosphate food additives.',
    examples: 'potato, boiling and then draining, food additive, chemical food component, bread'
  },
  {
    name: 'Compositional Dietary Nutrition Ontology',
    prefix: 'CDNO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/CDNO',
    owlUrl: 'http://purl.obolibrary.org/obo/cdno.owl',
    scope: 'Dietary minerals, macro elements, and dietary phosphorus concentrations in foods.',
    examples: 'dietary phosphorus, macro element, concentration of dietary phosphorus in material entity'
  },
  {
    name: 'Medical Action Ontology',
    prefix: 'MAXO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/MAXO',
    owlUrl: 'http://purl.obolibrary.org/obo/maxo.owl',
    scope: 'Clinical interventions and human nutrition intake actions.',
    examples: 'nutrition intake'
  },
  {
    name: 'Environmental Conditions, Treatments and Exposures Ontology',
    prefix: 'ECTO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/ECTO',
    owlUrl: 'http://purl.obolibrary.org/obo/ecto.owl',
    scope: 'Human and biological exposures to dietary phosphate, phosphorus, and microbial treatments.',
    examples: 'exposure to Bacillus subtilis, exposure to phosphate, exposure to phosphorus via ingestion, exposure to inorganic phosphate, exposure to phosphorus'
  },
  {
    name: 'Clinical Measurement Ontology',
    prefix: 'CMO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/CMO',
    owlUrl: 'http://purl.obolibrary.org/obo/cmo.owl',
    scope: 'Neutral quantitative physiological and clinical biomarker measurements.',
    examples: 'serum phosphate level, blood parathyroid hormone level, plasma ionized calcium level, body height, blood phosphate level'
  },
  {
    name: 'Ontology of Biological Attributes',
    prefix: 'OBA',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/OBA',
    owlUrl: 'http://purl.obolibrary.org/obo/oba.owl',
    scope: 'Biological and physiological trait attributes bridging anatomy and clinical measurements.',
    examples: 'amount of vitamin D, growth trait, amount of fibroblast growth factor 23, carotid artery intima-media region thickness'
  },
  {
    name: 'Human Phenotype Ontology',
    prefix: 'HP',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/HP',
    owlUrl: 'http://purl.obolibrary.org/obo/hp.owl',
    scope: 'Clinical abnormalities and human phenotypic signs associated with mineral dysregulation.',
    examples: 'Nephropathy, Intratubular calcium phosphate casts, Hyperphosphatemia, Calcinosis, Nephrocalcinosis'
  },
  {
    name: 'Mondo Disease Ontology',
    prefix: 'MONDO',
    standard: 'Monarch / OBO Foundry',
    ontobeeUrl: 'https://ontobee.org/ontology/MONDO',
    owlUrl: 'http://purl.obolibrary.org/obo/mondo.owl',
    scope: 'Human disease classes linking dietary and serum phosphate exposure to clinical pathologies.',
    examples: 'nephrocalcinosis, chronic kidney disease, cardiovascular disorder, calcinosis'
  },
  {
    name: 'Neuro Behavior Ontology',
    prefix: 'NBO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/NBO',
    owlUrl: 'http://purl.obolibrary.org/obo/nbo.owl',
    scope: 'Behavioral processes and feeding/consumption behaviors imported via OBO Foundry alignment.',
    examples: 'food consumption'
  },
  {
    name: 'Ontology for Nutritional Studies',
    prefix: 'ONS',
    standard: 'OBO Foundry / ENPADASI',
    ontobeeUrl: 'https://ontobee.org/ontology/ONS',
    owlUrl: 'http://purl.obolibrary.org/obo/ons.owl',
    scope: 'Human diets, dietary patterns, and nutritional study interventions imported via FoodOn.',
    examples: 'diet by nutritional composition'
  },
  {
    name: 'Units of Measurement Ontology',
    prefix: 'UO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/UO',
    owlUrl: 'http://purl.obolibrary.org/obo/uo.owl',
    scope: 'Standardized metric and scientific units indexed alongside measurements.',
    examples: 'None matched in this run (indexed: unit, length unit, mass unit)'
  },
  {
    name: 'Crop Ontology — Potato',
    prefix: 'CO_330',
    standard: 'Crop Ontology Standard',
    ontobeeUrl: 'https://cropontology.org/rdf/CO_330',
    owlUrl: 'https://cropontology.org/rdf/CO_330',
    scope: 'Potato-specific agronomic variables, root lengths, biomass weights, and tuber quality traits.',
    examples: 'Biomass weight, Root length, Plant productivity, Yield (qualitative), Tuber overall quality'
  },
  {
    name: 'Protein Ontology',
    prefix: 'PR',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/PR',
    owlUrl: 'http://purl.obolibrary.org/obo/pr.owl',
    scope: 'Hormones and regulatory proteins involved in phosphate homeostasis.',
    examples: 'fibroblast growth factor 23'
  },
  {
    name: 'Relation Ontology & Basic Formal Ontology',
    prefix: 'RO / BFO',
    standard: 'OBO Foundry Standard',
    ontobeeUrl: 'https://ontobee.org/ontology/RO',
    owlUrl: 'http://purl.obolibrary.org/obo/ro.owl',
    scope: 'Approved causal, regulatory, compositional, and dietary-input object properties.',
    examples: 'positively regulates characteristic, negatively regulates characteristic, causes or contributes to condition, has input, has part'
  }
]

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

  if (!iri) {
    return (
      <article className="ontology-introduction">
        <h2>Reference Ontologies</h2>
        <p>Select a class or relation on the left, or search by name or ID, to view its definition and hierarchy.</p>
      </article>
    )
  }

  if (!term.data) return <article><Notice {...term} /></article>
  const t = term.data
  const extractionProperty = extractionProperties.get(t.iri)
  const termUse = t.kind === 'object_property'
    ? extractionProperty ? `${extractionProperty.alias} · Approved for extraction` : 'Reference property'
    : t.selectable ? 'Selectable established ontology term' : 'Supporting ancestor term'
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
        <span className="muted">{t.origin === 'project_schema' ? 'Application metadata' : termUse}</span>
      </p>

      {/* Ontobee-style Class Hierarchy */}
      <OntobeeHierarchy term={t} onSelect={onSelect} />

      <dl className="term-fields">
        <dt>Definition</dt>
        <dd>{t.definition || 'No textual definition supplied in this reference fragment.'}</dd>
            {!!t.obo_unique_labels?.length && <><dt>Upstream unique labels</dt><dd>{t.obo_unique_labels.join('; ')}</dd></>}
            {!!t.inverse_of?.length && <><dt>Inverse of</dt><dd>{t.inverse_of.map(i => <button key={i} type="button" className="term-link" onClick={() => onSelect(i)}>{label(i)}</button>)}</dd></>}
        {!!t.usage_notes?.length && <><dt>Upstream usage notes</dt><dd>{t.usage_notes.map(note => <p key={note}>{note}</p>)}</dd></>}
        {!!t.comments?.length && <><dt>Upstream comments</dt><dd>{t.comments.map(comment => <p key={comment}>{comment}</p>)}</dd></>}
        {t.direct_use_allowed === false && <><dt>Application use</dt><dd>Broader classification only; direct assertion is not permitted by upstream usage guidance.</dd></>}
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
            ? 'Imported from an established OBO Foundry ontology.'
            : t.origin === 'project_schema'
            ? 'Application bookkeeping metadata; not a scientific grounding class or relation.'
            : 'Locally imported reference module. Consult its recorded source and selection rationale.'}
        </p>
        <ul>
          {(t.sources || []).map(source => (
            <li key={source}><Link iri={source} /></li>
          ))}
        </ul>
      </section>
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
  const isObjectPropTab = kind === 'object_property'

  const selectedTerm = useData(selected ? `/ontology/term?iri=${encoded(selected)}` : null, revision)

  useEffect(() => {
    const term = selectedTerm.data
    if (term?.kind === 'class' || term?.kind === 'object_property') {
      setKind(term.kind)
    }
  }, [selectedTerm.data])

  useEffect(() => {
    const timer = setTimeout(() => { setSearch(query); setOffset(0) }, 250)
    return () => clearTimeout(timer)
  }, [query])

  const data = useData(
    isObjectPropTab ? null : `/ontology/terms?kind=class&q=${encoded(search)}&offset=${offset}&limit=40`,
    revision,
  )
  const matchingExtractionProperties = closedPredicateList.filter(property => (
    [property.alias, property.id, property.display_label].some(value => value.toLowerCase().includes(search.trim().toLowerCase()))
  ))

  return (
    <div className="ontology-view-container">
      {/* Top Table: Established Ontologies (collapsible dropdown, hidden by default) */}
      <section className="ontology-established-overview" style={{ marginBottom: 16 }}>
        <details>
          <summary style={{ cursor: 'pointer', fontWeight: 600, fontSize: 14 }}>
            Established reference ontologies ({establishedOntologies.length})
          </summary>
          <div style={{ marginTop: 8 }}>
            <p className="muted" style={{ margin: '3px 0 8px', fontSize: 13 }}>
              Classes are indexed from established OBO Foundry reference ontologies, and object properties reflect the approved RO/BFO extraction relations.
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
              const count = key === 'object_property' ? closedPredicateList.length : statusData?.term_counts?.[key]
              return (
                <button
                  key={key}
                  type="button"
                  className={kind === key ? 'active' : ''}
                  onClick={() => { setKind(key); setOffset(0) }}
                >
                  {count != null ? `${name} (${count.toLocaleString()})` : name}
                </button>
              )
            })}
          </div>

          <label className="search-label">
            Search terms
            <input
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder={isObjectPropTab ? 'Relation name, alias or identifier' : 'Name, synonym or identifier'}
            />
          </label>

          {!isObjectPropTab && <Notice {...data} />}

          <ul className="ontology-tree">
            {isObjectPropTab ? matchingExtractionProperties.map(property => (
              <li key={property.iri} className={selected === property.iri ? 'ontology-tree-row selected' : 'ontology-tree-row'}>
                <button type="button" className="term-link" onClick={() => onSelect(property.iri)}>
                  {property.display_label}
                </button>
                <small className="muted"> {property.alias} · {property.id}</small>
              </li>
            )) : data.data?.terms?.map(term => (
              <li key={term.iri} className={selected === term.iri ? 'ontology-tree-row selected' : 'ontology-tree-row'}>
                <button type="button" className="term-link" onClick={() => onSelect(term.iri)}>
                  {term.label}
                </button>
                <small className="muted"> {term.id}</small>
              </li>
            ))}
          </ul>

          {isObjectPropTab && !matchingExtractionProperties.length && <p className="muted">No matching extraction relations.</p>}
          {!isObjectPropTab && data.data && !data.data.total && <p className="muted">No matching terms in this reference.</p>}
          {!isObjectPropTab && data.data && <Pagination {...data.data} onChange={setOffset} />}
        </aside>

        <TermDetails iri={selected} onSelect={onSelect} revision={revision} />
      </div>
    </div>
  )
}
