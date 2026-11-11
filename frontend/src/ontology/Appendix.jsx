import { useState } from 'react'
import { extractionExample, groundingExample } from './appendixExamples.js'

export default function Appendix() {
  const [stage, setStage] = useState('extraction')
  const extraction = stage === 'extraction'
  const example = extraction ? extractionExample : groundingExample

  return (
    <section className="ontology-appendix" aria-label="Appendix">
      <div className="appendix-stage-picker" role="group" aria-label="Example stage">
        <button
          type="button"
          aria-pressed={extraction}
          onClick={() => setStage('extraction')}
        >
          1. Claim extraction
        </button>
        <button
          type="button"
          aria-pressed={!extraction}
          onClick={() => setStage('grounding')}
        >
          2. Ontology grounding
        </button>
      </div>

      <div style={{ margin: '14px 0' }}>
        <h3 style={{ marginBottom: 4 }}>{example.stageName}</h3>
        <p className="muted" style={{ margin: 0 }}>{example.description}</p>
      </div>

      <div className="appendix-section-box">
        <h4>System Prompt &amp; Instructions</h4>
        <pre className="appendix-code-block" aria-label="System prompt instructions">{example.systemPrompt}</pre>
      </div>

      <div className="appendix-example-columns">
        <article>
          <h4>LLM Input Payload</h4>
          {!extraction && example.input?.terms && (
            <p className="muted" style={{ fontSize: 12, margin: '0 0 8px' }}>
              Full approved schema included without shortlisting: {Object.keys(example.input.terms).length} classes (T1–T{Object.keys(example.input.terms).length}) and {Object.keys(example.input.relations).length} object properties (R1–R{Object.keys(example.input.relations).length}); sample claim C1 appears at the end.
            </p>
          )}
          <pre className="appendix-code-block" aria-label="LLM input payload">
            {extraction ? example.input : JSON.stringify(example.input, null, 2)}
          </pre>
        </article>
        <article>
          <h4>Model Structured Output (JSON)</h4>
          <pre className="appendix-code-block" aria-label="LLM output JSON">
            {JSON.stringify(example.output, null, 2)}
          </pre>
        </article>
      </div>

      {extraction ? (
        <div className="compact-evidence" style={{ marginTop: 20 }}>
          <p className="muted" style={{ fontSize: 13 }}>
            Sentence ID 1 links directly to the extracted target span in the PDF text. Original citations, pages, and surrounding reading context are deterministically retained by the pipeline, while subject_anchor and object_anchor record exact surface spans without requiring model hallucination.
          </p>
        </div>
      ) : (
        <div className="compact-evidence" style={{ marginTop: 20 }}>
          <p className="muted" style={{ fontSize: 13 }}>
            Short aliases are deterministically resolved back to canonical upstream ontology IRIs (T115 -&gt; PO:0009005 root, T122 -&gt; PO:0025025 root system, R1 -&gt; BFO:0000050 part of), preserving endpoint denotation and mapping specificity (exact, broader, narrower, related, or unresolved) independently of claim scope.
          </p>
        </div>
      )}
    </section>
  )
}
