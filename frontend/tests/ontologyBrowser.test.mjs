import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile, readdir } from 'node:fs/promises'
import { createServer } from 'vite'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

test('active entry point loads only the ontology browser and its minimal stylesheet', async () => {
  const entry = await readFile(new URL('../src/main.jsx', import.meta.url), 'utf8')
  assert.match(entry, /ontology\/OntologyApp/)
  assert.doesNotMatch(entry, /Graph|pathways\.css|base\.css/)
  for (const name of await readdir(new URL('../src/ontology/', import.meta.url))) {
    const source = await readFile(new URL(`../src/ontology/${name}`, import.meta.url), 'utf8')
    assert.doesNotMatch(source, /from ['"].*(?:GraphView|ResearchNetwork|cytoscape)|request\(['"]\/(?:graph|pathways|pathway-assessments)/)
  }
})

test('ontology browser offers hierarchy categories without a graph or dashboard metrics', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [] }, appType: 'custom' })
  try {
    const { default: App } = await server.ssrLoadModule('/src/ontology/OntologyApp.jsx')
    const html = renderToStaticMarkup(React.createElement(App))
    assert.match(html, /Classes/)
    assert.match(html, /Object properties/)
    assert.match(html, /Data properties/)
    assert.match(html, /Name, synonym or identifier/)
    assert.match(html, /Mapping review/)
    assert.match(html, /Reasoning/)
    assert.match(html, /Appendix/)
    assert.doesNotMatch(html, />Tester</)
    assert.match(html, /Source fidelity and external validation are paused/)
    assert.doesNotMatch(html, /<canvas|cytoscape|network-metrics|overview-card|Show graph/)
    const { Link } = await server.ssrLoadModule('/src/ontology/useData.jsx')
    assert.doesNotMatch(renderToStaticMarkup(React.createElement(Link, { iri: 'javascript:alert(1)' })), /href=/)
  } finally { await server.close() }
})

test('published viewer hides execution and settings but preserves saved results', async () => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true, entries: [] }, appType: 'custom' })
  try {
    const { default: Runs } = await server.ssrLoadModule('/src/ontology/Runs.jsx')
    const html = renderToStaticMarkup(React.createElement(Runs, {
      pdfs: [], health: { capabilities: { execute: false, read_only: true } }, onRefresh() {},
    }))
    assert.doesNotMatch(html, /Run pipeline|New extraction and grounding|Ground saved claims|Extraction and model settings|type="file"/)
    assert.match(html, /Source fidelity and external scientific validation stay paused/)
  } finally { await server.close() }
})
