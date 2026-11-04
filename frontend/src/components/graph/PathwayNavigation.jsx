import { GitBranch, Network, ShieldCheck } from 'lucide-react'

export default function PathwayNavigation({ value, onChange, trustCount, assembledCount, healthCount, assessmentCount, pathwayOnly = false }) {
  return <nav className="pathway-navigation" aria-label="Research pathway views">
    {[
      ['assessments', ShieldCheck, 'Pathway assessments', 'Whole chains & exact evidence', assessmentCount],
      ['trust', Network, pathwayOnly ? 'Provisional chains' : 'Historical trust network', 'Source-grounded candidate connections', trustCount],
      ['assembled', GitBranch, pathwayOnly ? 'Supported pathways' : 'Historical pathway assembly', 'Specific assessed routes', assembledCount],
      ...(!pathwayOnly ? [['corroborated', ShieldCheck, 'Historical health paths', 'Earlier claim-level results', healthCount]] : []),
    ].map(([key, Icon, label, description, count]) => <button key={key} type="button"
      className={value === key ? 'active' : ''} aria-pressed={value === key} onClick={() => onChange(key)}>
      <span className="pathway-nav-icon"><Icon size={18} /></span>
      <span><strong>{label}</strong><small>{description}</small></span>
      <span className="pathway-nav-count" title="Loaded paths">{count ?? 0}</span>
    </button>)}
  </nav>
}
