"""Select small, verbatim document definitions; never rewrite scientific claims.

Verify context against the owning parsed packet, explicit document provenance,
or an individually verified abbreviation sentence.
There is no network access, ontology selection, inference or LLM call here.
"""
import re

from backend.domain.abbreviations import document_key, relevant_definitions
from backend.domain.treatment_labels import CODE, document_treatment_labels
from backend.shared.storage import stable_hash
from backend.shared.text import source_surface_mention

POLICY = 'document-definition-context-v2'
MAX_WORDS = 250
MAX_SENTENCES_PER_ROLE = 2
DEFINITION = re.compile(r'\b(?:means?|denotes?|refers? to|represents?|reflects?|measures?|'
    r'is defined|are defined|which defines|defined as|expressed as|labelled as|labeled as|designated as)\b', re.I)
VARIABLE = re.compile(r'\b(?:levels?|concentrations?|doses?|rates?|amounts?)\b', re.I)
SETTING = re.compile(r'\b(?:subjected|supplied|containing|adjusted|varied|varying|set at|labelled|labeled)\b', re.I)
QUANTITY = re.compile(r'\d+(?:\.\d+)?\s*(?:[munµμk]?g|[munµμk]?mol|[munµμk]?l|%|ppm|ppb)\b', re.I)


def definition_context_stale(row):
    return row.get('definition_context_policy') not in {None, 'disabled', POLICY}


def _labelled_setups(claim, packets, document, pdf_hash):
    codes = {role: {m[0].casefold() for m in CODE.finditer(claim.get(role + '_source_mention') or claim[role])}
             for role in ('subject', 'object')}
    if not any(codes.values()):
        return {}
    definitions = {}
    located = {(p.get('packet_number'), n, stable_hash(s['text'], 64)): s.get('source_sentence_id')
               for p in packets for n, s in enumerate(p.get('sentences', ()), 1)}
    for row in document_treatment_labels(packets, document, pdf_hash):
        label = row['label'].casefold()
        if not any(label in role_codes for role_codes in codes.values()):
            continue
        definitions.setdefault(label, {}).setdefault(row['text_sha256'], row)
    result = {}
    for role, role_codes in codes.items():
        if len(role_codes) != 1:
            continue
        matches = definitions.get(next(iter(role_codes)), {})
        if len(matches) != 1:
            continue
        setup = next(iter(matches.values()))
        text = setup['text']
        if not (SETTING.search(text) and QUANTITY.search(text)):
            continue
        key = (setup['packet_number'], setup['sentence_number'], setup['text_sha256'])
        result[role] = {**setup, 'source_sentence_id': located[key], 'selection_basis': 'treatment_label'}
    return result


def contexts_for_claim(claim, packets, glossary):
    document = document_key(claim)
    pdf_hash = claim.get('source_pdf_sha256')
    if not claim.get('source_document_id') or not pdf_hash:
        return {}
    inherited = relevant_definitions(claim, glossary)
    source = claim.get('source_context_packet')
    candidates, methods_packets, seen = [], [], set()
    for packet in packets:
        number = packet.get('packet_number')
        sentences = packet.get('sentences') or []
        owning = (number == claim.get('source_packet_number') and source
                  and source == ' '.join(s['text'] for s in sentences))
        # Explicitly tagged foreign packets never become local evidence even if
        # their text happens to repeat the same sentence.
        if packet.get('source_document_id', document) != document or packet.get('source_pdf_sha256', pdf_hash) != pdf_hash:
            continue
        if owning or (packet.get('source_document_id') == document and packet.get('source_pdf_sha256') == pdf_hash):
            methods_packets.append(packet)
        for n, sentence in enumerate(sentences, 1):
            text = sentence['text']
            digest = stable_hash(text, 64)
            verified_alias = any(r.get('document') == document and not r.get('ambiguous')
                and r.get('packet_number') == number and r.get('sentence_number') == n
                and r.get('context_sha256') == digest and r.get('quote') and r['quote'] in text for r in inherited)
            if not (owning or verified_alias) or (number, n, digest) in seen:
                continue
            seen.add((number, n, digest))
            candidates.append({'document': document, 'source_pdf_sha256': pdf_hash,
                'packet_number': number, 'sentence_number': n, 'page': sentence.get('page'),
                'source_sentence_id': sentence.get('source_sentence_id'), 'text': text, 'text_sha256': digest})
    treatment_setups = _labelled_setups(claim, methods_packets, document, pdf_hash)
    result, used, words = {}, {}, 0
    for role in ('subject', 'object'):
        names = {str(claim.get(role) or ''), str(claim.get(role + '_source_mention') or '')}
        names.discard('')
        setup = treatment_setups.get(role)
        ranked = [(-1, setup['packet_number'] or 0, setup['sentence_number'], setup)] if setup else []
        for record in candidates:
            if setup and record['text_sha256'] == setup['text_sha256']:
                continue
            text = record['text']
            if not any(source_surface_mention(text, name) for name in names):
                continue
            descriptive = bool(DEFINITION.search(text))
            variable_setup = bool(VARIABLE.search(text) and SETTING.search(text) and QUANTITY.search(text))
            if not (descriptive or variable_setup):
                continue
            # Text is a candidate explanation, not an automatically established
            # definition. The grounding model must inspect its actual meaning.
            basis = 'variable_setup' if variable_setup else 'explicit_description'
            labelled = variable_setup and re.search(r'\b(?:labelled as|labeled as|designated as|defined as)\b', text, re.I)
            ranked.append((0 if labelled else 1 if variable_setup else 2, record['packet_number'] or 0,
                           record['sentence_number'], {**record, 'selection_basis': basis}))
        selected = []
        for _, _, _, record in sorted(ranked, key=lambda x: x[:3]):
            key = (record['packet_number'], record['sentence_number'], record['text_sha256'])
            extra = 0 if key in used else len(record['text'].split())
            if words + extra > MAX_WORDS:
                continue  # Never cut a sentence, measurement, sign or charge.
            selected.append(record)
            used[key] = record
            words += extra
            if len(selected) == MAX_SENTENCES_PER_ROLE:
                break
        if selected:
            result[role] = selected
    return result


def definition_contexts(claims, packets, glossary):
    return {claim['candidate_id']: contexts_for_claim(claim, packets, glossary) for claim in claims}
