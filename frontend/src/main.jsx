import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './ontology/OntologyApp.jsx'
import './styles/ontology.css'

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
