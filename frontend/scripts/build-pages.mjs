import { cpSync, existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const dataSrc = resolve(root, '..', 'deploy', 'pages', 'data')
const distDir = resolve(root, 'dist')
const dataDest = resolve(distDir, 'data')

if (!existsSync(resolve(dataSrc, 'manifest.json'))) {
  console.error('Missing static export at deploy/pages/data/manifest.json. Run: python -m backend.cli.export_static --include-pdfs')
  process.exit(1)
}

const base = process.env.VITE_BASE_PATH || '/Crop-Health-to-Human-Health-Ontology-Pipeline/'
const env = {
  ...process.env,
  VITE_STATIC_VIEWER: '1',
  VITE_BASE_PATH: base.endsWith('/') ? base : `${base}/`,
}

const viteBin = resolve(root, 'node_modules', 'vite', 'bin', 'vite.js')
execFileSync(process.execPath, [viteBin, 'build'], { cwd: root, env, stdio: 'inherit' })

mkdirSync(dataDest, { recursive: true })
cpSync(dataSrc, dataDest, { recursive: true })
writeFileSync(resolve(distDir, '.nojekyll'), '')
console.log(`Static GitHub Pages build ready at ${distDir} (base=${env.VITE_BASE_PATH})`)
