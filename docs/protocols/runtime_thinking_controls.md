# Runtime thinking controls

Run details > Run settings > Assigned models provides independent On/Off switches for extraction, source validation, query planning, external judgement and pathway assembly. Every switch defaults to Off.

RunOptions.thinking contains five strictly boolean stage fields. Omitted stages default to false; unknown stages and non-boolean values are rejected. Each configured stage sends native thinking.type=enabled or disabled through the same fixed primary model. The same setting applies to its bounded syntax repair/regeneration calls. No model fallback or generation-budget reduction is introduced.

The chosen modes are saved with job options, run summaries/checkpoints and request metrics. Request progress shows the requested mode. Provider-reported reasoning tokens remain separate; positive reasoning usage despite Off generates a visible warning. An enabled request can legitimately report zero reasoning tokens.

Fresh runs apply all five controls. Resume uses the current controls, preserving saved page/target/extraction limits. Changing extraction thinking requires a fresh run because resume reuses extracted claims. Changing source/query thinking invalidates those stage caches; query changes also invalidate downstream retrieval/judgement caches. Changing external thinking invalidates external verdict reuse but preserves compatible retained paper packets. Changing assembly thinking rebuilds its entity mappings rather than seeding mappings from another mode. Historical checkpoints without thinking metadata retain their original On interpretation.

Claim repair, single-claim external retries and the manual assembly button receive these settings too. Existing scientific evidence/quotation guards remain in force. The external switch controls both per-paper judgement and cross-paper synthesis.

Offline verification covers per-stage request payloads, defaults, strict API validation, recovery propagation, worker forwarding, cache/retained-packet behaviour, original extraction policy and the five frontend switches. This change does not establish equal scientific accuracy between On and Off.
