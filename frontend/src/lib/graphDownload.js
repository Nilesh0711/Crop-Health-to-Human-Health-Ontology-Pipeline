export function downloadCanvasPng(cy, filename = 'research-network.png') {
  if (!cy || typeof window === 'undefined') return
  try {
    const dataUrl = cy.png({ full: true, scale: 2, bg: '#ffffff' })
    const link = document.createElement('a')
    link.download = filename
    link.href = dataUrl
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
  } catch (err) {
    console.error('Failed to export graph PNG:', err)
  }
}

export function downloadGraphJson(graph, filename = 'research-network.json') {
  if (!graph || typeof window === 'undefined') return
  try {
    const blob = new Blob([JSON.stringify(graph, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.download = filename
    link.href = url
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
    URL.revokeObjectURL(url)
  } catch (err) {
    console.error('Failed to export graph JSON:', err)
  }
}

