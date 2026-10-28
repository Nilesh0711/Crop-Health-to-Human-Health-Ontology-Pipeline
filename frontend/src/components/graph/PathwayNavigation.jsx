import { GitBranch, Network, ShieldCheck } from 'lucide-react'

export default function PathwayNavigation({ value, onChange, trustCount, assembledCount, healthCount }) {
  return <nav className="pathway-navigation" aria-label="Research pathway views">
    {[
      ['trust', Network, 'Trust network', 'Provisional claims & evidence', trustCount],
      ['assembled', GitBranch, 'Global pathway assembly', 'Eligible validated edges', assembledCount],
      ['corroborated', ShieldCheck, 'Corroborated health paths', 'Connections to human health', healthCount],
    ].map(([key, Icon, label, description, count]) => <button key={key} type="button"
      className={value === key ? 'active' : ''} aria-pressed={value === key} onClick={() => onChange(key)}>
      <span className="pathway-nav-icon"><Icon size={18} /></span>
      <span><strong>{label}</strong><small>{description}</small></span>
      <span className="pathway-nav-count" title="Loaded paths">{count ?? 0}</span>
    </button>)}
  </nav>
}
