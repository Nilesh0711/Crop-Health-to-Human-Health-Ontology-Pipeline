export const THINKING_STAGES = [
  ['extraction', 'Claim extraction'], ['source', 'Source validation'], ['query', 'Query planning'],
  ['external', 'External judgement'], ['pathway_assembly', 'Pathway assembly'],
]
export const defaultThinking = () => Object.fromEntries(THINKING_STAGES.map(([key]) => [key, false]))
export function resumeWithThinking(saved, current) {
  return { ...saved,
    ...(current.knowledge_building_mode ? { knowledge_building_mode: current.knowledge_building_mode } : {}),
    ...(current.trust_batch_size ? { trust_batch_size: current.trust_batch_size } : {}),
    thinking: { ...defaultThinking(), ...current.thinking } }
}
