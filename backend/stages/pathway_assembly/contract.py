"""MiMo Flash aligns entity occurrences; it does not write new claim edges."""

SYSTEM = """Assemble a global research graph by aligning entity occurrences in existing triplets.
Use the supplied source sentences, local context, headings, source mentions and qualifiers to resolve
pronouns, abbreviations, singular/plural forms and equivalent names across papers. Abstract example:
'this affected material' may refer to Crop C material explicitly identified in the preceding sentence.
Placeholder names illustrate the rule, never supply evidence. Preserve any source-stated infection restriction on its original edge. Do not merge completely
different crops, unrelated chemicals, or distinct plant organs. Category membership is not entity identity.

CRITICAL CANNOT-LINK CONSTRAINT:
Every incoming endpoint has a request-local claim_id (C1, C2, ...) and a role ('subject' or 'object').
NEVER assign the subject and object of the SAME claim edge (endpoints sharing the same claim_id) to the same group_id,
and never merge an endpoint into an existing registry node that already contains the opposite endpoint of its own claim edge.
Alignment connects endpoints ACROSS DISTINCT claims (e1 != e2) to build multi-hop pathways.
Merging the subject and object of the same claim collapses the relation into an invalid self-loop (e.g., merging
'expansion of wheat-maize rotation and conservation tillage practices' into 'Fusarium head blight' corrupts the agricultural
cause into the disease itself; similarly, never collapse the subject and descriptive predicate of a copular statement such as
'False smut' is 'one of the most important grain diseases of rice' into one node).
Keep the subject and object of every single claim strictly distinct.

Within the scientific context of a paper, align endpoints ACROSS DISTINCT claims that refer to the same biological
structure, pathogen, disease, or food-security impact to establish continuous causal pathways:
1. Head-noun and appositive structures across claims: Descriptive phrases and their core structures refer to the same
entity across claims (e.g., Claim 1 produces 'clusters of sporangia forming smut balls' <-> Claim 2 'smut balls' affects;
Claim 1 produces 'mycotoxins that may be produced in infected grains' <-> Claim 2 'toxins produced by the false smut pathogen'
when referring to false smut toxins; 'a large number of plant pathogens' <-> 'Fusarium graminearum / causal organisms').
Never merge the subject and object of the same claim.
2. Outcome-to-subject transitions: When an upstream edge states that a disease causes, alters, or threatens
a crop property (e.g. 'threatens nutritional value of food'), and a subsequent edge describes the agronomic
or socioeconomic consequences of that change (e.g. 'reduction of nutritive value' or 'plant disease epidemics
altering nutritive value of food' linked to reduced stocks/stability), align them to the shared impact node
('nutritional value of food / nutritive value reduction') while preserving each edge's specific relation.
3. Pathogen, disease, and lesion coreference: When a pathogen and its specific disease name or lesion process
are used co-referentially within the same document section (e.g., 'Phytophthora infestans' <-> 'potato late blight';
'Coffee rust' <-> 'spore production by coffee rust lesions'; 'Ustilaginoidea virens' <-> 'false smut of rice'),
align them so that agronomic drivers, pathogen biology, and downstream grain impacts form a continuous chain.
4. Contaminant accumulation and human population exposure: When claims in the same agricultural/regional setting
link farming practices to the accumulation of specific mycotoxins ('accumulation of fumonisins, ochratoxins, and aflatoxins'
in South Africa, 'aflatoxins' in West Africa) and follow with human exposure ('populations in maize-based subsistence
farming in South Africa and commercial groundnut production in West Africa' exposed to major health risks),
align the mycotoxin accumulation and population exposure referents to connect the pathway from crop management
to human health consequences.

This stage aligns endpoints and joins existing edges. It does not assess causal-chain correctness,
study compatibility or external support. Keep all supplied validation statuses unchanged. Do not
invent a new relation, reverse an edge, infer A->C, or omit an incoming endpoint. A->B and B->C remain
two original edges and form A->B->C. No health endpoint is required for a partial pathway.

Use the request-local endpoint IDs E1, E2, ... and registry node IDs N1, N2, ... exactly as supplied.
These IDs apply only to this request; never reconstruct long IDs or reuse IDs from another request.
Return endpoint_assignments: an object with EVERY supplied E ID exactly once as a key. Its value
is ONE integer group_id for the entity identity selected for that occurrence. An occurrence cannot
belong to both a broad category group and a specific entity group. Decide its actual referent from
the supplied context; keep uncertain identity separate. Never return endpoint membership lists.
Return registry_assignments: an object with EVERY supplied N ID exactly once as a key. Use the
same integer group_id when joining that existing entity, or null to leave it unchanged. Multiple
existing nodes may join one group when their contexts establish the same entity. Groups can contain
incoming endpoints, existing nodes, or both. Never guess nodes absent from this registry shard.
Code visits the selected global registry shards. If an incoming endpoint has a provisional_node_id, assign
it AND that N ID to the SAME group_id. Do not split an established identity group across shards.
Different fragments of one registry node are additional contexts for the same entity, not new nodes.
Return groups: exactly one canonical_label and explanation for every USED integer group_id.
Return groups FIRST, with a short evidence-based identity explanation before each canonical label
and joins_existing_registry_node declaration. This declaration is true exactly when at least one
registry N ID belongs to that group, including registry-only groups; otherwise false. A separation
decision must leave the rejected N ID null or in another group, never in the separated endpoint's
group. Then output endpoint_assignments and registry_assignments consistently with those decisions.
Explanation ordering is a response convention, not a request for hidden reasoning or a substitute
for the exact assignments. State the concise evidence-based decision only.
Do not return metadata for unused groups. Source evidence/local context fields refer to source_texts
by evidence_ref/local_context_ref. Read those exact supplied texts; references are not summaries.
canonical_label must name the entity, not summarize a chain. explanation describes identity alignment,
not scientific validation. Return the schema only."""

SYSTEM += (" Registry nodes may be a retrieved shortlist rather than the complete global registry. "
           "Similarity and lexical matches nominate candidates; they do not prove identity. "
           "Reject unrelated candidates even when names/types match. Use supplied claim_text together "
           "with exact source context and qualifiers. Keep uncertain referents separate; a new group "
           "does not prove that no equivalent entity exists outside this request.")

def response_schema(endpoint_ids, registry_ids):
    """One scalar assignment per supplied identity; membership lists cannot overlap."""
    group_limit = len(endpoint_ids) + len(registry_ids)
    group_id = {'type': 'integer', 'minimum': 1, 'maximum': group_limit}
    registry_group = {**group_id, 'type': ['integer', 'null']}
    return {
        'type': 'object', 'additionalProperties': False,
        'required': ['groups', 'endpoint_assignments', 'registry_assignments'],
        'properties': {
            'groups': {'type': 'array', 'minItems': 1, 'maxItems': group_limit, 'items': {
                'type': 'object', 'additionalProperties': False,
                'required': ['group_id', 'explanation', 'canonical_label', 'joins_existing_registry_node'],
                'properties': {
                    'group_id': group_id,
                    'explanation': {'type': 'string', 'maxLength': 1200},
                    'canonical_label': {'type': 'string', 'minLength': 1, 'maxLength': 200},
                    'joins_existing_registry_node': {'type': 'boolean'},
                },
            }},
            'endpoint_assignments': {'type': 'object', 'additionalProperties': False,
                'properties': {key: group_id for key in endpoint_ids}, 'required': list(endpoint_ids)},
            'registry_assignments': {'type': 'object', 'additionalProperties': False,
                'properties': {key: registry_group for key in registry_ids}, 'required': list(registry_ids)},
        },
    }
