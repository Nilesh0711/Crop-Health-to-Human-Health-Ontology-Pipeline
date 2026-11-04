import { colorClass, statusLabels } from '../../lib/claimPresentation.js'

export default function StatusPill({ value, children }) {
  return <span className={`status-pill ${colorClass(value)}`}><span className="status-dot" />{children || statusLabels[value] || value}</span>
}

