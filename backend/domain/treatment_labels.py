"""Document-local experimental labels, selected verbatim, never interpreted."""
import re

from backend.shared.storage import stable_hash

POLICY = 'source-defined-treatment-labels-v1'
LABEL_CUE = re.compile(r'\b(?:labelled|labeled|designated|denoted)\s+(?:as\s+)?', re.I)
SETTING = re.compile(r'\b(?:levels?|doses?|concentrations?|treatments?|experimental conditions?)\b', re.I)
# Only simple codes from an explicit labelling statement. Formulae with
# interleaved elements/charges and long gene identifiers are not guessed codes.
CODE = re.compile(r'(?<![\w.+-])[A-Za-z]{1,4}\d+(?:\.\d+)?(?![\w+-]|\.\d)')


def document_treatment_labels(packets, document_id, pdf_sha256):
    found = {}
    for packet in packets:
        for number, sentence in enumerate(packet.get('sentences', []), 1):
            text = sentence['text']
            cue = LABEL_CUE.search(text)
            if not cue or not SETTING.search(text[:cue.start()]):
                continue
            prefix = text[max(0, cue.start() - 90):cue.start()]
            if re.search(r'\b(?:genes?|transcripts?|cultivars?|genotypes?|strains?|vitamins?|formulae?|formulas?)\s+(?:were\s+|are\s+)?$', prefix, re.I):
                continue
            tail = text[cue.end():].split(';', 1)[0]
            for match in CODE.finditer(tail):
                label = match[0]
                key = (label.casefold(), packet.get('packet_number'), number)
                found[key] = {'label': label, 'document': document_id, 'source_pdf_sha256': pdf_sha256,
                    'packet_number': packet.get('packet_number'), 'sentence_number': number,
                    'page': sentence.get('page'), 'text': text, 'text_sha256': stable_hash(text, 64), 'policy': POLICY}
    return list(found.values())


def labels_for_claim(claim, labels):
    words = {str(claim.get(k) or '').strip().casefold() for k in
             ('subject', 'object', 'subject_source_mention', 'object_source_mention')}
    return [r for r in labels if r['label'].casefold() in words]
