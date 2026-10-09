import { useEffect, useRef, useState } from 'react'
import cytoscape from 'cytoscape'
import { Focus, Maximize2, Network } from 'lucide-react'

const nodeColors = {
  Crop: '#6B9B6E', PlantDisease: '#DA8069', Nutrient: '#C8A751',
  Contaminant: '#9A6D57', HumanExposure: '#557FA6', HumanHealthOutcome: '#C36E7A',
  SoilCondition: '#A98270', SoilBiota: '#568B7C', PlantProcess: '#8FAD61', PlantTrait: '#769B63',
  PlantPest: '#C38C55', ClimateFactor: '#6C9CB8', HumanBiologicalProcess: '#7C78AD',
  AgriculturalPractice: '#8E81B1',
}

const edgeColors = {
  extracted: '#B9C6C3', single_source: '#D7AD52', corroborated: '#279378',
  contradicted: '#D96B68', contested: '#BB6C95', insufficient: '#A8B1B1', rejected: '#8E9694', error: '#9FA6AD',
  semantic_rejected: '#C76C76', semantic_needs_review: '#B08B65',
  source_unsupported: '#C97871', source_needs_review: '#B99B64',
}

export default function GraphView({ graph, activeFilter, onSelect }) {
  const canvas = useRef(null)
  const instance = useRef(null)
  const [layout, setLayout] = useState('cose')
  const visibleEdges = (graph?.edges || []).filter(edge => activeFilter === 'all' || edge.status === activeFilter)
  const visibleNodes = (graph?.nodes || []).filter(node => visibleEdges.some(edge => edge.source === node.id || edge.target === node.id))

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
          'label': 'data(label)', 'font-family': 'DM Sans', 'font-size': 11, 'font-weight': 700,
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
        : { name: 'cose', animate: false, randomize: false, idealEdgeLength: 115, nodeRepulsion: 420000, padding: 50 },
      wheelSensitivity: 0.2,
      minZoom: 0.2,
      maxZoom: 3,
    })
    cy.on('tap', 'node', event => onSelect({ kind: 'node', ...event.target.data() }))
    cy.on('tap', 'edge', event => onSelect({ kind: 'edge', ...event.target.data() }))
    cy.on('tap', event => { if (event.target === cy) onSelect(null) })
    instance.current = cy
    return () => cy.destroy()
  }, [graph, activeFilter, layout])

  if (!visibleNodes.length) {
    return <div className="graph-empty"><span className="empty-icon"><Network size={30} /></span><h3>No graph claims to show</h3>
      <p>{graph?.edges?.length ? 'Try another evidence filter.' : 'Run a PDF to extract claims and build its graph.'}</p></div>
  }

  return <div className="graph-stage">
    <div className="graph-toolbar">
      <button type="button" className="tool-button" onClick={() => setLayout(value => value === 'cose' ? 'breadthfirst' : 'cose')} title="Switch graph layout"><Focus size={16} /> {layout === 'cose' ? 'Organic' : 'Flow'}</button>
      <button type="button" className="icon-button" onClick={() => instance.current?.fit(undefined, 40)} title="Fit graph to screen"><Maximize2 size={17} /></button>
    </div>
    <div ref={canvas} className="graph-canvas" />
    <div className="graph-hint">Drag nodes · Scroll to zoom · Select a claim for details</div>
  </div>
}
