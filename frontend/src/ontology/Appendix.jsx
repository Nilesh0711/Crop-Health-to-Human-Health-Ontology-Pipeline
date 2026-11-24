import { useState } from 'react'
import {
  closedExtractionExample,
  entityGroundingExample,
  crossPaperMergingExample,
  closedAtomicExtractionExample,
  closedComparisonAbstentionExample,
} from './appendixExamples.js'

const EXAMPLES = {
  closedExtraction: closedExtractionExample,
  entityGrounding: entityGroundingExample,
  crossPaperMerging: crossPaperMergingExample,
}

const stripVersionTags = text => String(text || '').replace(/\s*\(closed-ro-bfo-\d+-v\d+\)/gi, '')

export default function Appendix() {
  const [stage, setStage] = useState('closedExtraction')
  const extraction = stage === 'closedExtraction'
  const grounding = stage === 'entityGrounding'
  const merging = stage === 'crossPaperMerging'
  const example = EXAMPLES[stage]

  return (
    <section className="ontology-appendix" aria-label="Appendix">
      <section className="appendix-overview" aria-label="Pipeline methodology overview">
        <div className="appendix-overview-grid">
          <div className="appendix-stage-card">
            <h3>Stage 1: Claim extraction</h3>
            <p>
              Reads PDF text packets and extracts verbatim atomic claims restricted to approved ontology relations (<code>P1</code> to <code>P12</code>) with study qualifiers, or returns <code>{`{"claims": []}`}</code>.
            </p>
          </div>
          <div className="appendix-stage-card">
            <h3>Stage 2: Entity grounding</h3>
            <p>
              Retrieves candidate OBO Foundry classes locally (<code>0</code> LLM calls) for subject and object mentions, then uses an LLM to select exact or broader terms while keeping the Stage 1 predicate fixed.
            </p>
          </div>
          <div className="appendix-stage-card">
            <h3>Stage 3: Cross-paper merging</h3>
            <p>
              Deterministically joins eligible grounded claims across papers (<code>0</code> LLM calls) using directional flow (<code>↓</code>) and class-link (<code>↔</code>) arrows, food/nutrient bridging, and study-context checks.
            </p>
          </div>
        </div>
      </section>

      <div className="appendix-stage-picker" role="group" aria-label="Prompt and output examples">
        <button
          type="button"
          aria-pressed={extraction}
          onClick={() => setStage('closedExtraction')}
        >
          1. Claim extraction
        </button>
        <button
          type="button"
          aria-pressed={grounding}
          onClick={() => setStage('entityGrounding')}
        >
          2. Entity grounding
        </button>
        <button
          type="button"
          aria-pressed={merging}
          onClick={() => setStage('crossPaperMerging')}
        >
          3. Cross-paper merging
        </button>
      </div>

      <section className="appendix-stage-panel">
        <header className="appendix-stage-header">
          <h3>
            {extraction ? 'Stage 1: Claim extraction'
              : grounding ? 'Stage 2: Entity grounding'
                : 'Stage 3: Cross-paper merging'}
          </h3>
          <p className="muted">
            {extraction
              ? 'Selects one approved ontology relation per claim and quotes the supporting text.'
              : grounding
                ? 'Maps subject and object mentions to established OBO Foundry classes while keeping the extracted relation fixed.'
                : example.description}
          </p>
        </header>

        {example.summary && (
          <div className="appendix-prose-block" aria-label="How this stage works">
            <h4>How this stage works</h4>
            <div className="ontology-table-scroll">
              <table className="appendix-table">
                <tbody>
                  <tr><th style={{ width: '18%' }}>Input</th><td>{example.summary.input}</td></tr>
                  <tr><th>Method</th><td>{example.summary.method}</td></tr>
                  <tr><th>Guardrails</th><td>{example.summary.guardrails}</td></tr>
                  <tr><th>Output</th><td>{example.summary.output}</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        )}

        {grounding && (
          <div className="appendix-retrieval-section" aria-label="Entity candidate retrieval pipeline">
            <div className="appendix-prose-block">
              <h4>Candidate Class Retrieval</h4>
              <p>
                Candidate selection runs locally and deterministically with 0 LLM calls, producing at most 14 candidates per entity.
              </p>

              <ol className="appendix-step-list">
                <li>
                  <strong>Query preparation:</strong> Expands paper abbreviations, infers the entity category, links named cultivars or treatment codes to crop taxon or PECO exposure classes, and pairs bare food-mineral mentions (like <code>phosphorus</code>) with dietary nutrient classes (<code>CDNO:0000018</code>).
                </li>
                <li>
                  <strong>Hybrid retrieval:</strong> Searches four local channels in parallel: normalized lexical and synonym lookup, SQLite FTS5 BM25, dense semantic embeddings (BGE), and exact ChEBI formula matching.
                </li>
                <li>
                  <strong>Rank fusion and parents:</strong> Combines channel ranks with weighted Reciprocal Rank Fusion (<code>k = 35</code>) and adds direct ontology parents of top hits for broader fallback matches.
                </li>
                <li>
                  <strong>Filtering and type slots:</strong> Drops deprecated or mismatched classes and reserves up to 6 slots for classes matching the fixed relation domain or range.
                </li>
                <li>
                  <strong>LLM grounding:</strong> Passes the top 14 candidates (<code>T1</code> to <code>T14</code>) with definitions, synonyms, parents, and compatible type lists to the LLM to choose <code>exact</code>, <code>broader</code>, <code>narrower</code>, <code>related</code>, or <code>unresolved</code>.
                </li>
              </ol>
            </div>

            <div className="appendix-trace-box" aria-label="Entity retrieval worked example">
              <h4>Worked example</h4>
              <div className="appendix-claim-summary">
                <p>
                  <strong>Claim:</strong> <code>Phosphorus deficiency</code> →{' '}
                  <code>negatively regulates characteristic (RO:0019002, P2)</code> → <code>root growth</code>
                </p>
                <p className="muted">
                  <strong>Required types:</strong> Subject domain <code>process</code> (<code>PECO</code>, <code>GO</code>); Object range <code>characteristic</code> (<code>TO</code>, <code>CO_330</code>, <code>PATO</code>).
                </p>
              </div>

              <p className="appendix-table-caption">
                Four local retrieval channels (0 LLM calls)
              </p>
              <div className="ontology-table-scroll">
                <table className="appendix-table">
                  <thead>
                    <tr>
                      <th>Channel</th>
                      <th>Weight</th>
                      <th>Top hits for &ldquo;Phosphorus deficiency&rdquo;</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td>Lexical and synonym</td>
                      <td><code>0.90</code></td>
                      <td>
                        <code>#1 TO:0000102</code> (phosphorus response), <code>#2 PECO:0007397</code> (phosphorus nutrient exposure),{' '}
                        <code>#5 PECO:0007104</code> (NPK fertilizer exposure)
                      </td>
                    </tr>
                    <tr>
                      <td>SQLite FTS5 BM25</td>
                      <td><code>0.70</code></td>
                      <td>
                        <code>#13 GO:0006793</code> (phosphorus metabolic process), <code>#18 TO:0001025</code> (leaf phosphorus content),{' '}
                        <code>#20 ECTO:9001227</code> (exposure to phosphorus)
                      </td>
                    </tr>
                    <tr>
                      <td>Dense semantic (BGE)</td>
                      <td><code>1.60</code></td>
                      <td>
                        <code>#1 PECO:0007397</code>, <strong><code>#2 PECO:0001046</code> (limited phosphate exposure)</strong>,{' '}
                        <code>#3 PECO:0007100</code> (superphosphate exposure), <code>#4 GO:0016036</code> (cellular response to phosphate starvation)
                      </td>
                    </tr>
                    <tr>
                      <td>ChEBI formula</td>
                      <td><code>None</code></td>
                      <td>No match (not a chemical formula)</td>
                    </tr>
                  </tbody>
                </table>
              </div>

              <ul className="appendix-bullet-list">
                <li>
                  <strong>Why dense retrieval helps:</strong> Lexical and BM25 miss <code>PECO:0001046</code> because it shares no words with &ldquo;Phosphorus deficiency&rdquo;, while BGE ranks it <code>#2</code> and surfaces <code>TO:0000656</code> (root development trait) at <code>#17</code> for &ldquo;root growth&rdquo;.
                </li>
                <li>
                  <strong>Fusion and parents:</strong> <code>RRF(c) = sum(weight / (35 + rank))</code> scores <code>PECO:0007397</code> at <code>0.0688</code> (<code>#1</code>) and <code>PECO:0001046</code> at <code>0.0432</code> (<code>#7</code>), and adds parent classes <code>PECO:0007240</code> and <code>PECO:0001006</code>.
                </li>
                <li>
                  <strong>Type slot reservation:</strong> Reserving up to 6 compatible slots keeps <code>TO:0000656</code> (<code>#11</code>) and <code>CO_330</code> root traits inside the top 14 ahead of <code>GO</code> process hits.
                </li>
                <li>
                  <strong>Selection and verification:</strong> The LLM selects <code>PECO:0001046</code> (broader, state) and <code>TO:0000656</code> (broader, concept). Deterministic checks confirm deficiency wording and verify <code>process</code> and <code>characteristic</code> ancestry.
                </li>
              </ul>
            </div>
          </div>
        )}

        {!merging && (
          <div className="appendix-example-columns">
            <article className="appendix-code-panel">
              <h4>Prompt Input</h4>
              <pre className="appendix-code-block" aria-label="LLM input payload">
                {extraction ? example.input : JSON.stringify(example.input, null, 2)}
              </pre>
            </article>
            <article className="appendix-code-panel">
              <h4>Structured Output (JSON)</h4>
              <pre className="appendix-code-block" aria-label="LLM output JSON">
                {JSON.stringify(example.output, null, 2)}
              </pre>
            </article>
          </div>
        )}

        {merging && (
          <div className="appendix-retrieval-section" aria-label="Cross-paper merging methodology">
            <div className="appendix-prose-block">
              <h4>Deterministic Merging Rules (0 LLM calls)</h4>
              <ol className="appendix-step-list">
                {example.rules.map((rule, idx) => <li key={idx}>{rule}</li>)}
              </ol>
            </div>
            <div className="appendix-trace-box" aria-label="Cross-paper merging worked example">
              <h4>{example.workedExample.title}</h4>
              <ol className="appendix-step-list">
                {example.workedExample.steps.map((step, idx) => <li key={idx}>{step}</li>)}
              </ol>
              <p className="muted">{example.workedExample.blockedNote}</p>
            </div>
          </div>
        )}

        {grounding && example.aliasExplanation && (
          <p className="muted appendix-alias-note">
            <strong>Resolved identifiers:</strong> {example.aliasExplanation}
          </p>
        )}

        {!merging && (
          <details className="appendix-section-box">
            <summary>System prompt and instructions</summary>
            <pre className="appendix-code-block" aria-label="System prompt instructions">
              {stripVersionTags(example.systemPrompt)}
            </pre>
          </details>
        )}
      </section>

      {extraction && (
        <div className="appendix-example-columns appendix-subcards">
          <article className="appendix-subcard" aria-label="Closed-relation atomic example">
            <h4>{closedAtomicExtractionExample.title}</h4>
            <blockquote className="appendix-quote">{closedAtomicExtractionExample.source}</blockquote>
            <pre className="appendix-code-block">{JSON.stringify(closedAtomicExtractionExample.output, null, 2)}</pre>
            <p className="muted">{closedAtomicExtractionExample.explanation}</p>
          </article>
          <article className="appendix-subcard" aria-label="Unsupported comparison example">
            <h4>{closedComparisonAbstentionExample.title}</h4>
            <blockquote className="appendix-quote">{closedComparisonAbstentionExample.source}</blockquote>
            <pre className="appendix-code-block">{JSON.stringify(closedComparisonAbstentionExample.output, null, 2)}</pre>
            <p className="muted">{closedComparisonAbstentionExample.explanation}</p>
          </article>
        </div>
      )}
    </section>
  )
}
