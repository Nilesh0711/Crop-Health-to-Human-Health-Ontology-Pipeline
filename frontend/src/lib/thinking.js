export const THINKING_STAGES = [
  ['extraction', 'Claim extraction'], ['source', 'Source validation'], ['query', 'Query planning'],
  ['external', 'External judgement'], ['pathway_assembly', 'Pathway assembly'],
]
export const defaultThinking = () => Object.fromEntries(THINKING_STAGES.map(([key]) => [key, false]))
export function resumeWithThinking(saved, current) {
  return { ...saved, thinking: { ...defaultThinking(), ...current.thinking } }
}
