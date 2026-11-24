"""Isolated ontology-constrained extraction and mapping in the same model call."""

import json
import re
from contextlib import closing, nullcontext
from copy import deepcopy

from backend.domain.extraction_review import extraction_warnings, mapping_meaning_warnings
from backend.evaluation.ontology_lookup import (
    ENDPOINT_POLICY, POLICY as LOOKUP_POLICY, endpoint_candidates, open_lookup, passage_candidates,
)
from backend.ontology.property_types import check_property_types, verifiable_upper_types
from backend.providers.structured_output import SchemaValidationError
from backend.shared.artifacts import save_json, save_rows
from backend.shared.cancellation import check_cancelled
from backend.shared.storage import stable_hash
from backend.shared.text import normalized, word_count
from backend.stages.claim_extraction.anchoring import endpoint_anchor
from backend.stages.claim_extraction.contract import EXTRACTION_OUTPUT_TOKENS, EXTRACTION_RESPONSE_SCHEMA
from backend.stages.claim_extraction.targeting import build_extraction_targets, check_target_evidence
from backend.stages.ontology_grounding.vocabulary import catalog_term_map
from backend.stages.pdf_parsing.constants import CITATION_GROUP_RE, DOI_RE, PARENTHETICAL_BLOCK_RE
from backend.stages.pdf_parsing.packets import iter_pdf_context_packets


TARGET_WORDS = 160
NEIGHBOR_WORDS = 65
ROLES = ('subject', 'relation', 'object')
UNRESOLVED = 'unresolved'
_NULL_EFFECT = re.compile(
    r'\b(?:not\b.{0,55}\bsignifican\w*|no\s+(?:statistically\s+)?significant|no\s+(?:such\s+)?(?:relationship|correlation|association)s?|not\s+considerable)\b',
    re.I,
)
_SIGNIFICANCE_COMPARISON = re.compile(r'\bsignificant\w*\b.*\b(?:differ\w*|between|for|higher|lower)\b|'
                                      r'\b(?:differ\w*|higher|lower)\b.*\bsignificant\w*\b', re.I)
_CORRELATION = 'http://purl.obolibrary.org/obo/RO_0002610'
_CAUSAL_GROUP = 'http://purl.obolibrary.org/obo/RO_0002410'
_AUTHOR_YEAR = re.compile(
    r"[A-Z][\w’'\-]+(?:(?:\s+et\s+al\.|\s+(?:and|&)\s+[A-Z][\w’'\-]+)"
    r"(?:\s*,\s*|\s+)|\s*,\s*)(?:19|20)\d{2}[a-z]?"
)
_INLINE_CITATION = re.compile(
    r"\b[A-Z][\w’'\-]+(?:\s+et\s+al\.|\s+(?:and|&)\s+[A-Z][\w’'\-]+)"
    r"\s*\(\s*(?:19|20)\d{2}[a-z]?\s*\)"
)

INSTRUCTIONS = """Return source-supported, in-scope claims that you can extract and ground together.

1. Express one finding per claim. Choose two distinct, source-worded scientific endpoints and a
short predicate with the correct direction. Split independently reported outcomes and retain genuine
joint treatments and compound traits. Keep defining details such as efficiency, chemical form, and
the organ measured. Put experimental conditions, comparators, values and units in qualifiers.
For "Treatment T increased Trait Q and Trait R," write one finding for each trait with its own value.
2. Match the scientific kind first. Distinguish a treatment event, the substance administered,
and its measured outcomes. Choose subject_term and object_term from the supplied classes and
relation_term from the supplied properties. Apply the definitions, usage notes and domain/range,
including their stated causal requirements. Preserve negation, uncertainty and comparison.
3. Use exact for equivalent meaning. List a role in broader when the supplied term genuinely
includes the endpoint or represents a weaker relationship implied by the claim. Keep the specific
meaning in the wording. Return a claim when all three bindings qualify, or leave it out.
4. Give ascending evidence_sentence_ids with at least one target sentence. Use neighboring
sentences to substantiate that finding and its qualifiers. Return {"claims": []} for an empty target."""

LOOKUP_INSTRUCTIONS = """Extract each in-scope, source-supported finding and map its three roles.

1. Copy subject and object wording from the cited sentences, including abbreviations, singular/plural forms,
and defining words. Reuse shared source words when separating independently stated outcomes. Write a short
predicate with the reported direction, negation and uncertainty. Keep treatments, groups, measurements and
comparators in the qualifiers when they are context for the finding.
2. Fill the term fields separately. Read the candidate definition and parents. The source phrases used to
retrieve terms are search hints. Choose a term only when its scientific kind and meaning apply in context.
Record the role in broader when the chosen meaning is genuinely more general.
3. Check the relation's domain, range, usage notes and the strength of the reported finding. Use unresolved
in any term field without a suitable candidate. Keep the source finding and its other mappings.
4. Cite at least one target sentence. Cite a neighboring sentence only when it supports the same finding.
Return {"claims": []} when the target passage has no in-scope finding."""


def citation_free(text):
    def parenthetical(match):
        parts = match['content'].split(';')
        return ' ' if all(_AUTHOR_YEAR.fullmatch(part.strip()) for part in parts) else match[0]

    text = PARENTHETICAL_BLOCK_RE.sub(parenthetical, text)
    text = _INLINE_CITATION.sub(' ', text)
    text = DOI_RE.sub(' ', CITATION_GROUP_RE.sub(' ', text))
    return re.sub(r'\s+([,.;:])', r'\1', ' '.join(text.split()))


def local_sentences(target):
    sentences = target['sentences']
    owned = target['target_sentence_numbers']
    origin = sentences[owned[0] - 1]
    visible = {number: citation_free(sentences[number - 1]['text']) for number in owned}
    for number in (owned[0] - 1, owned[-1] + 1):
        if not 1 <= number <= len(sentences):
            continue
        row = sentences[number - 1]
        if row.get('appended_context') or any(row.get(k) != origin.get(k) for k in ('chapter_id', 'section_heading')):
            continue
        text = citation_free(row['text'])
        if text and word_count(text) <= NEIGHBOR_WORDS:
            visible[number] = text
    return {number: text for number, text in sorted(visible.items()) if text}


def request_setup(reference, selection=None):
    catalog, terms, class_ids, properties, property_ids = catalog_term_map(reference)
    # Keep the upstream meanings and restrictions. Omit transport/provenance fields
    # and related synonyms, which are suggestions rather than identity evidence.
    terms = {alias: {k: v for k, v in term.items() if k != 'related_synonyms'}
             for alias, term in terms.items()}
    properties = {alias: {k: v for k, v in prop.items() if not k.endswith('_provenance')}
                  for alias, prop in properties.items()}
    if selection is not None:
        projected, matched, hints = selection
        terms, class_ids = {}, {}
        for index, (iri, phrase) in enumerate(matched.items(), 1):
            alias = f'T{index}'
            term = projected.terms[iri]
            terms[alias] = {'label': term['label'], 'iri': iri,
                            'parents': [projected.terms.get(parent, {}).get('label', parent)
                                        for parent in term.get('parents', [])],
                            'lookup_match': phrase.removeprefix('~')}
            if term.get('definition'):
                terms[alias]['definition'] = term['definition']
            if upper := verifiable_upper_types(projected, iri):
                terms[alias]['upper_types'] = upper
            class_ids[alias] = iri
    vocabulary = json.dumps({'classes': terms, 'properties': properties},
                            ensure_ascii=False, separators=(',', ':'))
    instructions = INSTRUCTIONS if selection is None else LOOKUP_INSTRUCTIONS
    system = f"{instructions}\n\n{catalog.extraction_instructions()}\n\nApproved ontology vocabulary:\n{vocabulary}"
    if selection is not None and hints:
        system += '\n\nSource-defined abbreviation hints: ' + json.dumps(hints, ensure_ascii=False)
    schema = deepcopy(EXTRACTION_RESPONSE_SCHEMA)
    item = schema['properties']['claims']['items']
    for name in ('subject_type', 'object_type', 'subject_anchor', 'object_anchor'):
        item['properties'].pop(name)
    for field in item['properties'].values():
        field.pop('description', None)
    for field in item['properties']['qualifiers']['properties'].values():
        field.pop('description', None)
    aliases = {'subject': class_ids, 'relation': property_ids, 'object': class_ids}
    for role, choices in aliases.items():
        key = f'{role}_term'
        item['properties'][key] = {'type': 'string', 'enum': [*choices, UNRESOLVED]
                                   if selection is not None else list(choices)}
        item['required'].append(key)
    item['properties']['broader'] = {
        'type': 'array', 'items': {'type': 'string', 'enum': list(ROLES)}, 'uniqueItems': True,
    }
    item['required'].append('broader')
    return catalog if selection is None else projected, system, schema, aliases


def relation_evidence_issue(predicate, binding, reference):
    """Protect reported null effects and significance-only comparisons."""
    iri = binding.get('iri')
    term = reference.terms.get(iri, {})
    parents = {parent['iri'] for parent in term.get('parent_properties', []) if parent.get('iri')}
    if (_NULL_EFFECT.search(predicate)
            and (iri in {_CAUSAL_GROUP, _CORRELATION} or _CAUSAL_GROUP in parents)):
        return 'non_significant_result_as_causal_or_correlated'
    if (iri == _CORRELATION and _SIGNIFICANCE_COMPARISON.search(predicate)
            and not re.search(r'\b(?:correlat\w*|associat\w*)\b', predicate, re.I)):
        return 'group_difference_as_correlation'
    return None


def grounded_claim(proposal, target, visible, catalog, aliases, diagnostics=None):
    evidence_ids = proposal['evidence_sentence_ids']
    evidence = [visible[number] for number in evidence_ids]
    if len(normalized(proposal['relation'])) < 2 or normalized(proposal['subject']) == normalized(proposal['object']):
        if diagnostics is not None:
            diagnostics['reason'] = 'invalid_roles'
        return None
    failed = [role for role in ('subject', 'object') if not endpoint_anchor(proposal[role], None, evidence)]
    if failed:
        if diagnostics is not None:
            diagnostics.update(reason='unanchored_endpoint', roles=failed)
        return None
    bindings = {}
    for role in ROLES:
        term_alias = proposal[f'{role}_term']
        if diagnostics is not None and term_alias == UNRESOLVED:
            bindings[role] = {'iri': None, 'label': None, 'mapping': UNRESOLVED}
            continue
        iri = aliases[role][term_alias]
        term = catalog.terms[iri]
        mapping = 'broader' if role in proposal['broader'] else 'exact'
        if mapping not in term.get('allowed_mappings', ('exact', 'broader')):
            if diagnostics is not None:
                bindings[role] = {'iri': None, 'label': None, 'mapping': UNRESOLVED}
                diagnostics.setdefault('mapping_holds', []).append({'role': role, 'code': 'restricted_property_usage'})
                continue
            return None
        bindings[role] = {'iri': iri, 'label': term['label'], 'mapping': mapping}
    claim = {name: proposal[name].strip() for name in ROLES}
    owned = set(target['target_sentence_numbers'])
    claim.update(qualifiers={key: value for key, value in proposal['qualifiers'].items() if value},
                 source_evidence_span=' '.join(evidence),
                 source_evidence_target_fragments=[{'sentence_number': number, 'text': visible[number]}
                                                   for number in evidence_ids if number in owned])
    assessment = {'original_claim': claim, 'bindings': bindings}
    type_check = check_property_types(assessment, catalog)
    meaning = mapping_meaning_warnings(assessment, catalog)
    warnings = extraction_warnings(claim)
    blocked = type_check['status'] in {'incompatible', 'missing_required_endpoint'}
    if diagnostics is None and (blocked or meaning or warnings):
        return None
    if diagnostics is not None:
        if issue := relation_evidence_issue(claim['relation'], bindings['relation'], catalog):
            meaning.append({'role': 'relation', 'code': issue})
        if blocked:
            meaning.append({'role': 'relation', 'code': type_check['status']})
        if meaning:
            diagnostics.setdefault('mapping_holds', []).extend(meaning)
            for issue in meaning:
                bindings[issue['role']] = {'iri': None, 'label': None, 'mapping': UNRESOLVED}
            recheck = check_property_types(assessment, catalog)
            if recheck['status'] == 'missing_required_endpoint':
                bindings['relation'] = {'iri': None, 'label': None, 'mapping': UNRESOLVED}
                diagnostics['mapping_holds'].append({'role': 'relation', 'code': recheck['status']})
        if warnings:
            diagnostics['extraction_warnings'] = warnings
    page = target['sentences'][next(number for number in evidence_ids if number in owned) - 1]['page']
    return {**{k: claim[k] for k in (*ROLES, 'qualifiers')}, 'bindings': bindings,
            'evidence_sentence_ids': evidence_ids, 'source_evidence_span': claim['source_evidence_span'],
            'target_id': target['target_id'], 'source_page': page,
            **({'classified': not warnings and all(bindings[role]['iri'] for role in ROLES),
                'review': 'held' if warnings else 'not_flagged',
                **({'diagnostics': diagnostics} if diagnostics else {})} if diagnostics is not None else {})}


def source_targets(pdf, *, with_abbreviations=False):
    # Target preparation can look through packets while it is still assigning
    # targets. Reuse the full collection so lookahead never consumes later pages.
    packets = list(iter_pdf_context_packets(pdf))
    return build_extraction_targets(packets, TARGET_WORDS, abbreviation_glossary=None if with_abbreviations else [])


def run_single_stage(pdf, reference, client, output, progress, budget, *, prepared_targets=None, lookup_dir=None):
    targets = source_targets(pdf, with_abbreviations=lookup_dir is not None) if prepared_targets is None else prepared_targets
    if not targets:
        raise ValueError('The PDF has no extractable text.')
    if lookup_dir is None:
        setup = request_setup(reference)
        save_json(output / 'prompt.json', dict(zip(('system', 'response_schema', 'aliases'), setup[1:])))
        lookup = nullcontext(None)
    else:
        db, signature = open_lookup(lookup_dir)
        lookup = closing(db)
        setup = None
        save_json(output / 'prompt.json', {'lookup_policy': LOOKUP_POLICY, 'lookup_signature': signature,
                                          'endpoint_lookup_policy': ENDPOINT_POLICY})
        progress(lookup_policy=LOOKUP_POLICY, lookup_signature=signature, endpoint_lookup_policy=ENDPOINT_POLICY)
    with lookup as db:
        run_targets(targets, reference, setup, db, client, output, progress, budget)


def run_targets(targets, reference, setup, db, client, output, progress, budget):
    rows, rejections, seen = [], [], set()
    lookup_rejections, endpoint_results = [], []
    progress(total_targets=len(targets))
    for index, target in enumerate(targets, 1):
        check_cancelled()
        visible = local_sentences(target)
        owned = set(target['target_sentence_numbers']) & visible.keys()
        if owned:
            passage = '\n'.join(f"{number} {'target' if number in owned else 'neighbor'}: {line}"
                                for number, line in visible.items())
            if db is None:
                catalog, system, schema, aliases = setup
            else:
                selection = passage_candidates(db, reference, visible, owned, target.get('source_abbreviation_definitions', []))
                catalog, system, schema, aliases = request_setup(reference, selection)
                save_json(output / 'lookup_requests' / f'{index:02d}.json',
                          {'target_id': target['target_id'], 'system': system, 'passage': passage,
                           'response_schema': schema, 'aliases': aliases, 'class_matches': selection[1]})
            answer = client.chat_json(system, passage, max_tokens=min(budget, EXTRACTION_OUTPUT_TOKENS), json_schema=schema,
                                      request_label=f'single-stage target {index}/{len(targets)}')
            for proposal in answer['claims']:
                try:
                    check_target_evidence({'claims': [proposal]}, visible.keys(), owned)
                except SchemaValidationError as error:
                    # A proposal belonging to a neighboring target must not invalidate
                    # the valid proposals from this response or trigger another request.
                    rejections.append({'target_id': target['target_id'], 'target_number': index,
                                       'reason': str(error), 'proposal': proposal})
                    continue
                diagnostics = None if db is None else {}
                row = grounded_claim(proposal, target, visible, catalog, aliases, diagnostics)
                if row is None:
                    if diagnostics is not None:
                        lookup_rejections.append({'target_id': target['target_id'], 'target_number': index,
                                                  'proposal': proposal, **diagnostics})
                    continue
                identifier = stable_hash(json.dumps(row, sort_keys=True, ensure_ascii=False), 32)
                if identifier not in seen:
                    seen.add(identifier)
                    rows.append({'id': identifier, **row})
                    if db is not None:
                        evidence = [visible[number] for number in row['evidence_sentence_ids']]
                        endpoints = {role: endpoint_candidates(
                            db, reference, row[role], evidence, target.get('source_abbreviation_definitions', ()))
                            for role in ('subject', 'object')}
                        endpoint_results.append({'claim_id': identifier, 'target_id': row['target_id'],
                                                 'endpoints': endpoints})
            save_rows(output / 'claims.jsonl', rows)
            if endpoint_results:
                save_rows(output / 'endpoint_candidates.jsonl', endpoint_results)
            if rejections:
                save_rows(output / 'evidence_rejections.jsonl', rejections)
            if lookup_rejections:
                save_rows(output / 'proposal_rejections.jsonl', lookup_rejections)
        progress(completed_targets=index, claim_count=len(rows), evidence_rejection_count=len(rejections))
