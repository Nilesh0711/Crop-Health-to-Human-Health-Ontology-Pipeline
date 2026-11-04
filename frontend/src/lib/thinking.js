export const THINKING_STAGES = [
  ['extraction', 'Claim extraction'], ['source', 'Source validation'], ['query', 'Query planning'],
  ['external', 'External judgement'], ['pathway_assembly', 'Pathway assembly'],
]
export const defaultThinking = () => Object.fromEntries(THINKING_STAGES.map(([key]) => [key, false]))
export function resumeWithThinking(saved, current) {
  const { pathway_max_paths, pathway_max_hops, ...compatible } = saved
  return { ...compatible,
    neighbor_max_pairs: current.neighbor_max_pairs ?? 100,
    articles_per_pathway: current.articles_per_pathway ?? saved.articles_per_pathway ?? saved.articles_per_claim ?? 8,
    thinking: { ...defaultThinking(), ...current.thinking } }
}
