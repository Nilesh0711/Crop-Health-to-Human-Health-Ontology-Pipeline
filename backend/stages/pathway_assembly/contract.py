"""MiMo Flash aligns entity occurrences; it does not write new claim edges."""

SYSTEM = """Assemble a global research graph by aligning entity occurrences in existing triplets.
Use the supplied source sentences, local context, headings, source mentions and qualifiers to resolve
pronouns, abbreviations, singular/plural forms and equivalent names across papers. Abstract example:
'this affected material' may refer to Crop C material explicitly identified in the preceding sentence.
Placeholder names illustrate the rule, never supply evidence. Preserve any source-stated infection
restriction on its original edge. Do not merge different crops, chemicals, measured properties or
plant organs. A material and a disease affecting it are different entities. A chemical's nutrient
or contaminant role can differ by occurrence; do not equate distinct substances merely by their role.

This stage aligns endpoints and joins existing edges. It does not assess causal-chain correctness,
study compatibility or external support. Keep all supplied validation statuses unchanged. Do not
invent a new relation, reverse an edge, infer A->C, or omit an incoming endpoint. A->B and B->C remain
two original edges and form A->B->C. No health endpoint is required for a partial pathway.

Return groups of equivalent entity occurrences. Each incoming endpoint_id must appear exactly once.
existing_node_ids can reference only registry node IDs supplied in this request. Multiple existing
nodes may be joined when their contexts establish the same entity. Uncertain identity stays separate.
Groups may contain just incoming endpoints, just existing nodes to merge, or both. Nodes absent from
the registry shard must never be guessed. All global registry shards will be considered by code.
If an incoming endpoint has a provisional_node_id, its group MUST include that node ID in
existing_node_ids. Join that established group to another registry node if appropriate; do not split
an existing identity group while scanning shards. Different fragments of one registry node carry
the same node_id and supply additional occurrence contexts, not new entities.
canonical_label must name the entity, not summarize a chain. explanation describes identity alignment,
not scientific validation. Return the schema only."""

RESPONSE_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['groups'],
    'properties': {'groups': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False,
        'required': ['canonical_label', 'endpoint_ids', 'existing_node_ids', 'explanation'],
        'properties': {
            'canonical_label': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'endpoint_ids': {'type': 'array', 'items': {'type': 'string'}},
            'existing_node_ids': {'type': 'array', 'items': {'type': 'string'}},
            'explanation': {'type': 'string', 'maxLength': 1200},
        },
    }}},
}
