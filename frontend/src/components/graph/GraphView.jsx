import { useEffect, useMemo, useRef, useState } from 'react'
import cytoscape from 'cytoscape'
import { ArrowRight, Download, Focus, Lock, Maximize2, Minus, Network, Plus, RefreshCw, SlidersHorizontal, Unlock, X } from 'lucide-react'
import { downloadCanvasPng, downloadGraphJson } from '../../lib/graphDownload.js'
import { statusLabels } from '../../lib/claimPresentation.js'
import { entityLabel, readable } from '../../lib/pathways.js'
import { highlightNetwork, rememberPositions, syncNetwork, visibleNetwork } from '../../lib/graphNetwork.js'

const nodeColors = {
  Crop: '#8DB39A', PlantDisease: '#CE9B8D', Nutrient: '#C9BB88',
  Contaminant: '#B39A88', HumanExposure: '#8BA8C1', HumanHealthOutcome: '#CB9FA5',
  SoilCondition: '#BCA58F', SoilBiota: '#8AB0A1', PlantProcess: '#B0BE91', PlantTrait: '#A0B796',
  PlantPest: '#C7AB87', ClimateFactor: '#94B7C7', HumanBiologicalProcess: '#ACA6C5',
  AgriculturalPractice: '#B1A6C3', SoilMaterial: '#BAC5A8', SoilChemicalProperty: '#B6B594', FoodProduct: '#C2B598',
  PlantPathogen: '#C7AB87', PathogenTrait: '#A0B796', Unresolved: '#B9C6C3',
}

const edgeColors = {
  extracted: '#B9C6C3', single_source: '#D7AD52', corroborated: '#279378', source_reference_supported: '#8D78B7',
  contradicted: '#D96B68', contested: '#BB6C95', insufficient: '#A8B1B1', not_found: '#A8B1B1', rejected: '#8E9694', error: '#9FA6AD',
  local_connection_supported: '#279378', source_supported: '#D7AD52', pathway_supported: '#279378',
  semantic_rejected: '#C76C76', semantic_needs_review: '#B08B65',
  source_unsupported: '#C97871', source_needs_review: '#B99B64',
  out_of_scope: '#8E9694',
  external_needs_review: '#B99B64',
}

export default function GraphView({ graph, activeFilter, onSelect, onViewClaims, emptyTitle, emptyDescription, emptyAction,
  compactControls = false, highlightedEdgeIds = [] }) {
  const canvas = useRef(null)
  const instance = useRef(null)
  const positions = useRef(new Map())
  const viewport = useRef(null)
  const select = useRef(onSelect)
  select.current = onSelect
  const initialLayout = useRef(false)
  const lastArrangement = useRef(null)
  const runningLayout = useRef(null)
  const [layout, setLayout] = useState('cose')
  const [arrangement, setArrangement] = useState(0)
  const [locked, setLocked] = useState(false)
  const [showRelations, setShowRelations] = useState(!compactControls)
  const [showLegend, setShowLegend] = useState(!compactControls)
  const highlightedKey = highlightedEdgeIds.join('|')
  const network = useMemo(() => visibleNetwork(graph, activeFilter), [graph, activeFilter])
  const { nodes: visibleNodes, edges: visibleEdges } = network
  const hasNodes = visibleNodes.length > 0

  function zoomBy(factor) {
    const cy = instance.current
    if (!cy) return
    cy.zoom({ level: Math.max(cy.minZoom(), Math.min(cy.maxZoom(), cy.zoom() * factor)),
      renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } })
  }

  useEffect(() => {
    if (!canvas.current || !hasNodes) return
    const cy = cytoscape({
      container: canvas.current,
      elements: [],
      style: [
        { selector: 'node', style: { 'background-color': 'data(color)', 'border-width': 3, 'border-color': '#ffffff',
          'label': 'data(label)', 'font-family': 'DM Sans', 'font-size': 12, 'font-weight': 600,
          'color': '#263B36', 'text-wrap': 'wrap', 'text-max-width': 110, 'text-valign': 'bottom',
          'text-margin-y': 12, 'width': 23, 'height': 23 } },
        { selector: 'edge', style: { 'curve-style': 'bezier', 'line-color': 'data(color)',
          'target-arrow-color': 'data(color)', 'target-arrow-shape': 'triangle', 'arrow-scale': 0.75,
          'width': 2.3, 'opacity': 0.8, 'label': 'data(relation)', 'font-family': 'DM Sans', 'font-size': 8,
          'color': '#798D86', 'text-rotation': 'none', 'text-background-color': '#ffffff',
          'text-background-opacity': 0.86, 'text-background-padding': 2 } },
        { selector: ':selected', style: { 'overlay-color': '#2A8E74', 'overlay-opacity': 0.14,
          'overlay-padding': 8, 'border-color': '#173F36', 'border-width': 4 } },
        { selector: '.path-dimmed', style: { opacity: 0.18, 'text-opacity': 0.35 } },
        { selector: 'edge.path-active', style: { width: 4, opacity: 1, 'line-color': '#4e7794', 'target-arrow-color': '#4e7794', 'text-opacity': 1 } },
        { selector: 'node.path-active', style: { opacity: 1, 'text-opacity': 1, 'border-color': '#4e7794', 'border-width': 4 } },
      ],
      layout: { name: 'preset', fit: false },
      wheelSensitivity: 0.2,
      minZoom: 0.2,
      maxZoom: 3,
    })
    cy.on('tap', 'node', event => select.current?.({ kind: 'node', ...event.target.data() }))
    cy.on('tap', 'edge', event => select.current?.({ kind: 'edge', ...event.target.data() }))
    cy.on('tap', event => { if (event.target === cy) select.current?.(null) })
    cy.on('pan zoom', () => { viewport.current = { pan: { ...cy.pan() }, zoom: cy.zoom() } })
    cy.on('dragfree layoutstop', () => rememberPositions(cy, positions.current))
    if (viewport.current) cy.viewport(viewport.current)
    instance.current = cy
    const resizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
      if (canvas.current?.clientWidth && canvas.current?.clientHeight) cy.resize()
    })
    resizeObserver?.observe(canvas.current)
    return () => {
      resizeObserver?.disconnect()
      if (instance.current === cy) instance.current = null
      runningLayout.current?.stop()
      rememberPositions(cy, positions.current)
      cy.destroy()
    }
  }, [hasNodes])

  useEffect(() => {
    if (!instance.current || !hasNodes) return
    const selectedId = instance.current.$(':selected').first().id()
    const result = syncNetwork(instance.current, {
      nodes: network.nodes.map(node => ({ ...node, color: nodeColors[node.type] || '#7E9B92' })),
      edges: network.edges.map(edge => ({ ...edge, color: edgeColors[edge.status] || '#BBC5C1' })),
    }, positions.current)
    initialLayout.current ||= result.firstLayout
    if (selectedId) {
      const selected = instance.current.getElementById(selectedId)
      select.current?.(selected.nonempty() ? { kind: selected.isNode() ? 'node' : 'edge', ...selected.data() } : null, { refresh: true })
    }
  }, [network, hasNodes])

  useEffect(() => {
    const cy = instance.current
    const request = `${layout}:${arrangement}`
    if (!cy || !hasNodes || (!initialLayout.current && lastArrangement.current === request)) return
    initialLayout.current = false
    runningLayout.current?.stop()
    const options = layout === 'breadthfirst'
      ? { name: 'breadthfirst', directed: true, animate: false, spacingFactor: 1.35, padding: 45 }
      : { name: 'cose', animate: false, randomize: false, idealEdgeLength: 85, nodeRepulsion: 60000, componentSpacing: 65, padding: 40 }
    runningLayout.current = cy.layout({ ...options, stop: () => {
      // A disposed/StrictMode-interrupted layout must not masquerade as complete.
      if (instance.current === cy) lastArrangement.current = request
    } })
    runningLayout.current.run()
  }, [layout, arrangement, hasNodes])

  useEffect(() => { instance.current?.autoungrabify(locked) }, [locked, hasNodes])

  useEffect(() => {
    const cy = instance.current
    if (!cy || !hasNodes) return
    highlightNetwork(cy, highlightedKey ? highlightedKey.split('|') : [], showRelations)
  }, [network, hasNodes, highlightedKey, showRelations])

  if (!visibleNodes.length) {
    return <div className="graph-empty"><span className="empty-icon"><Network size={30} /></span><h3>{emptyTitle || 'No graph claims to show'}</h3>
      <p>{graph?.edges?.length ? 'Try another evidence filter.' : emptyDescription || 'No relations are available in this graph view. Inspect claims for source evidence, pending checks, and validation decisions.'}</p>
      {emptyAction}
      {onViewClaims && <button type="button" className="button secondary" onClick={onViewClaims}>Review claims</button>}</div>
  }

  return <div className="graph-stage">
    <div className="graph-toolbar">
      <button type="button" className="icon-button" aria-label="Zoom in on graph" title="Zoom in" onClick={() => zoomBy(1.25)}><Plus size={16} /></button>
      <button type="button" className="icon-button" aria-label="Zoom out of graph" title="Zoom out" onClick={() => zoomBy(.8)}><Minus size={16} /></button>
      <button type="button" className="icon-button" onClick={() => instance.current?.fit(undefined, 40)} title="Fit graph to screen" aria-label="Fit graph to screen"><Maximize2 size={17} /></button>
      {!!highlightedEdgeIds.length && <button type="button" className="tool-button" onClick={() => {
        const cy = instance.current
        if (cy) { const edges = cy.edges().filter(edge => highlightedEdgeIds.includes(edge.id())); if (edges.nonempty()) cy.fit(edges.union(edges.connectedNodes()), 60) }
      }}><Focus size={15} />Focus pathway</button>}
      <details className={`graph-display-menu ${compactControls ? 'compact' : ''}`}>
        <summary><Download size={15} />Export</summary><div>
          <button type="button" className="tool-button" onClick={() => downloadCanvasPng(instance.current, 'network-graph.png')} title="Download as PNG image"><Download size={14} />Save PNG</button>
          <button type="button" className="tool-button" onClick={() => downloadGraphJson(graph, 'network-graph.json')} title="Download graph as JSON"><Download size={14} />Save JSON</button>
        </div></details>
      <details className={`graph-display-menu ${compactControls ? 'compact' : ''}`} open={compactControls ? undefined : true}>
        <summary><SlidersHorizontal size={15} />Display</summary><div>
          <button type="button" className="tool-button" onClick={() => setLayout(value => value === 'cose' ? 'breadthfirst' : 'cose')} title="Switch graph layout"><Focus size={16} /> {layout === 'cose' ? 'Organic' : 'Flow'} layout</button>
          <button type="button" className="tool-button" onClick={() => setArrangement(value => value + 1)} title="Rearrange the graph only when requested"><RefreshCw size={15} />Rearrange</button>
          <button type="button" className="tool-button" aria-pressed={locked} onClick={() => setLocked(value => !value)} title={locked ? 'Allow dragging nodes' : 'Prevent accidental node movement'}>
            {locked ? <Lock size={15} /> : <Unlock size={15} />}{locked ? 'Nodes locked' : 'Lock nodes'}</button>
          <label><input type="checkbox" checked={showRelations} onChange={event => setShowRelations(event.target.checked)} />Show relation labels</label>
          <label><input type="checkbox" checked={showLegend} onChange={event => setShowLegend(event.target.checked)} />Show entity colour key</label>
        </div></details>
    </div>
    <div ref={canvas} className="graph-canvas" />
    <div className="graph-type-legend" aria-label="Entity types in this graph">
      <span className="graph-type-count">{visibleNodes.length} entities · {visibleEdges.length} relations</span>
      {showLegend && [...new Set(visibleNodes.map(node => node.type || 'Unresolved'))].sort().map(type => <span key={type}>
        <i style={{ background: nodeColors[type] || '#7E9B92' }} />{type.replace(/([a-z])([A-Z])/g, '$1 $2')}</span>)}
    </div>
    <div className="graph-hint">{locked ? 'Nodes locked' : 'Drag nodes to arrange'} · Pan background · Scroll to zoom</div>
  </div>
}


export function GraphSelectionDetails({ element, onClose, onInspectPathway, onInspectClaim }) {
  if (!element) return null
  const isEdge = element.kind === 'edge'
  const pathwayMember = isEdge && (['pathway_membership_only', 'not_individually_evaluated', 'local_connection_membership'].includes(element.external_status) || element.individually_externally_validated === false || element.supported_pathway_ids?.length > 0)
  const pathwayIds = element.supported_pathway_ids || []
  const paper = element.paper || element.document_key
  const statusText = statusLabels[element.status] || readable(element.status)
  const summaryParts = isEdge
    ? [
        statusText,
        pathwayMember
          ? (pathwayIds.length ? `in ${pathwayIds.length} evidence-supported ${element.external_status === 'local_connection_membership' ? 'local connections' : pathwayIds.length === 1 ? 'pathway' : 'pathways'} (edge not individually externally validated)` : 'not individually externally validated')
          : (element.support_count != null ? `${element.support_count} supporting article(s)` : null),
        paper,
      ].filter(Boolean).join(' · ')
    : [element.type, element.aliases?.length > 1 ? `Aligned names: ${element.aliases.join(', ')}` : null].filter(Boolean).join(' · ')
  return <div className="graph-selection">
    <button type="button" className="close-mini" aria-label="Close selected graph details" onClick={onClose}><X size={16} /></button>
    <span className="eyebrow">{isEdge ? 'SELECTED RELATION' : 'SELECTED ENTITY'}</span>
    <strong>{isEdge ? `${entityLabel(element, 'source')} → ${readable(element.relation)} → ${entityLabel(element, 'target')}` : element.label}</strong>
    <p>{summaryParts}</p>
    {element.rejection_reason && <p>{element.rejection_reason}</p>}
    {element.quote && <blockquote>“{element.quote}”</blockquote>}
    {isEdge && (pathwayIds.length > 0 || (onInspectClaim && element.candidate_id)) && <div className="graph-selection-actions">
      {onInspectClaim && element.candidate_id && <button type="button" className="text-button" onClick={() => onInspectClaim(element.candidate_id)}>Inspect source claim <ArrowRight size={13} /></button>}
      {onInspectPathway && pathwayIds.map(id => <button key={id} type="button" className="text-button" onClick={() => onInspectPathway(id)}>Inspect {id.slice(0, 16)}… <ArrowRight size={13} /></button>)}
    </div>}
  </div>
}
