import { useEffect, useRef, useState } from 'react'
import cytoscape from 'cytoscape'
import { Focus, Maximize2, Minus, Network, Plus } from 'lucide-react'

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
  source_supported: '#D7AD52',
  semantic_rejected: '#C76C76', semantic_needs_review: '#B08B65',
  source_unsupported: '#C97871', source_needs_review: '#B99B64',
  out_of_scope: '#8E9694',
  external_needs_review: '#B99B64',
}

export default function GraphView({ graph, activeFilter, onSelect, onViewClaims }) {
  const canvas = useRef(null)
  const instance = useRef(null)
  const [layout, setLayout] = useState('cose')
  const visibleEdges = (graph?.edges || []).filter(edge => activeFilter === 'all' || edge.status === activeFilter)
  const visibleNodes = (graph?.nodes || []).filter(node => visibleEdges.some(edge => edge.source === node.id || edge.target === node.id))

  function zoomBy(factor) {
    const cy = instance.current
    if (!cy) return
    cy.zoom({ level: Math.max(cy.minZoom(), Math.min(cy.maxZoom(), cy.zoom() * factor)),
      renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } })
  }

  useEffect(() => {
    if (!canvas.current || !visibleNodes.length) {
      instance.current?.destroy()
      instance.current = null
      return
    }
    instance.current?.destroy()
    const cy = cytoscape({
      container: canvas.current,
      elements: [
        ...visibleNodes.map(node => ({ data: { ...node, color: nodeColors[node.type] || '#7E9B92' } })),
        ...visibleEdges.map(edge => ({ data: { ...edge, color: edgeColors[edge.status] || '#BBC5C1' } })),
      ],
      style: [
        { selector: 'node', style: { 'background-color': 'data(color)', 'border-width': 3, 'border-color': '#ffffff',
          'label': 'data(label)', 'font-family': 'DM Sans', 'font-size': 12, 'font-weight': 600,
          'color': '#263B36', 'text-wrap': 'wrap', 'text-max-width': 110, 'text-valign': 'bottom',
          'text-margin-y': 12, 'width': 23, 'height': 23 } },
        { selector: 'edge', style: { 'curve-style': 'bezier', 'line-color': 'data(color)',
          'target-arrow-color': 'data(color)', 'target-arrow-shape': 'triangle', 'arrow-scale': 0.75,
          'width': 2.3, 'opacity': 0.8, 'label': 'data(relation)', 'font-family': 'DM Sans', 'font-size': 8,
          'color': '#798D86', 'text-rotation': 'autorotate', 'text-background-color': '#ffffff',
          'text-background-opacity': 0.86, 'text-background-padding': 2 } },
        { selector: ':selected', style: { 'overlay-color': '#2A8E74', 'overlay-opacity': 0.14,
          'overlay-padding': 8, 'border-color': '#173F36', 'border-width': 4 } },
      ],
      layout: layout === 'breadthfirst'
        ? { name: 'breadthfirst', directed: true, spacingFactor: 1.35, padding: 45 }
        : { name: 'cose', animate: false, randomize: false, idealEdgeLength: 85, nodeRepulsion: 60000, componentSpacing: 65, padding: 40 },
      wheelSensitivity: 0.2,
      minZoom: 0.2,
      maxZoom: 3,
    })
    cy.on('tap', 'node', event => onSelect({ kind: 'node', ...event.target.data() }))
    cy.on('tap', 'edge', event => onSelect({ kind: 'edge', ...event.target.data() }))
    cy.on('tap', event => { if (event.target === cy) onSelect(null) })
    instance.current = cy
    const resizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
      cy.resize()
      cy.fit(undefined, 40)
    })
    resizeObserver?.observe(canvas.current)
    return () => { resizeObserver?.disconnect(); cy.destroy() }
  }, [graph, activeFilter, layout])

  if (!visibleNodes.length) {
    return <div className="graph-empty"><span className="empty-icon"><Network size={30} /></span><h3>No graph claims to show</h3>
      <p>{graph?.edges?.length ? 'Try another evidence filter.' : 'No relations are available in this graph view. Inspect claims for source evidence, pending checks, and validation decisions.'}</p>
      {onViewClaims && <button type="button" className="button secondary" onClick={onViewClaims}>Review claims</button>}</div>
  }

  return <div className="graph-stage">
    <div className="graph-toolbar">
      <button type="button" className="tool-button" onClick={() => setLayout(value => value === 'cose' ? 'breadthfirst' : 'cose')} title="Switch graph layout"><Focus size={16} /> {layout === 'cose' ? 'Organic' : 'Flow'}</button>
      <button type="button" className="icon-button" aria-label="Zoom in on graph" title="Zoom in" onClick={() => zoomBy(1.25)}><Plus size={16} /></button>
      <button type="button" className="icon-button" aria-label="Zoom out of graph" title="Zoom out" onClick={() => zoomBy(.8)}><Minus size={16} /></button>
      <button type="button" className="icon-button" onClick={() => instance.current?.fit(undefined, 40)} title="Fit graph to screen" aria-label="Fit graph to screen"><Maximize2 size={17} /></button>
    </div>
    <div ref={canvas} className="graph-canvas" />
    <div className="graph-hint">Drag nodes · Scroll to zoom · Select a claim for details</div>
  </div>
}
