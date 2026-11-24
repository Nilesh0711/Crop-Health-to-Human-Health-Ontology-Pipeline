"""Matched, user-triggered comparison against production extraction and grounding."""

import json

from backend.domain.classification import classification_complete
from backend.evaluation.single_stage import ROLES, local_sentences, run_single_stage, source_targets
from backend.shared.artifacts import save_json, save_rows
from backend.shared.cancellation import check_cancelled
from backend.shared.storage import file_hash, read_jsonl, stable_hash
from backend.stages.claim_extraction.extractor import extract_claims
from backend.stages.ontology_grounding.grounder import ground_claims


PDF_NAME = 'Kirchgesser_2023_Potato_Root_Architecture_Phosphorus.pdf'
PDF_HASH = '4a87265be6179466759db12c8a2fa6c9fa8d0af09cf14bdc89fd37542ede222f'

# Expectations are for reviewers. Only the source window and general rules
# are supplied to the model.
CASES = (
    ('Abstract findings', 'Preserve acquisition efficiency as a trait. Retain only one of 22 traits and the low-P restriction.',
     'target-5c878d92cb366faba74d3b1a', 'bdcdc832de441451fcf1f90c5da2590a7d0d4c8652774ab38d079efb265fa5b0'),
    ('Independent outcomes', 'Keep growth, tuber quality and starch quality distinct. Check that the property matches the reported direction and strength.',
     'target-ebd4601aea4154fdf287a888', '5482ec8bdd215b07b187f2744f64429f911efc1f91bacc35ee10333f585a54fb'),
    ('Root growth under low phosphorus', 'Keep the overall finding and any separately reported traits distinct. Preserve the treatment and the scope of significance.',
     'target-c6c867226152482bd04e8291', '4e98ad1fd98bf2ad7654484d85dadbf371212938cd92d6aab3fae79c334f4b7e'),
    ('Rooting groups and percentages', 'Check the category, treatment and percentage together. Preserve the distinction between a category change and a causal effect.',
     'target-82ed030eb44865377503c86e', '61dc1830aa0fefdec5afc9e7296c13117ce9819fd281933a7e3d684c255463ff'),
    ('Length, surface area and dry mass', 'Keep length and surface area separate, preserve the treatment comparison, and distinguish the current experiment from cited background.',
     'target-d9d9c0fb08f7c733dbaf2cdf', '7e6d23b1c702681f36923d0eac80f1fd109b0374eebc0cba386c4b73e1dd8b15'),
    ('Non-significant root-to-shoot result', 'Preserve the ratio as one trait and the absence of a significant change. Keep speculative acquisition wording qualified.',
     'target-111b269c33fd7186a823be43', 'c6d226e11486e084e2ccb5534c367dbb380d4981b3650a9ed502407a2ac0205c'),
    ('Uptake comparisons', 'Keep the 19.5% and 23.7% comparisons with their correct baseline groups, low P and the separate experiment.',
     'target-2e5d80339f5b08d2540b37bf', '256096ea14b707bf81ddf3305eea65033a06d5b1e5f15af929b547c11038abdb'),
    ('Cultivar-specific findings', 'Keep the compared cultivars and the different low-P responses separate, with the scope of the previous experiment.',
     'target-4ae14c132edbd7f69a5e4242', '15986a667a7f1a713a96d72046ef17bc424a073ee9d63211232f677fec801a75'),
)


def prepare_sample(pdf, *, with_abbreviations=False):
    if file_hash(pdf) != PDF_HASH:
        raise ValueError('The comparison is frozen to the saved Kirchgesser PDF.')
    by_id = {target['target_id']: target for target in source_targets(pdf, with_abbreviations=with_abbreviations)}
    sample = []
    for title, review, target_id, passage_hash in CASES:
        original = by_id.get(target_id)
        if original is None:
            raise ValueError(f'The parsed comparison passage changed: {title}.')
        visible = local_sentences(original)
        if stable_hash(json.dumps(visible, ensure_ascii=False, sort_keys=True), 64) != passage_hash:
            raise ValueError(f'The local context of the comparison passage changed: {title}.')
        owned = set(original['target_sentence_numbers'])
        sentences = [{**original['sentences'][n - 1], 'text': text} for n, text in visible.items()]
        target_numbers = [i for i, n in enumerate(visible, 1) if n in owned]
        target = {**original, 'sentences': sentences, 'target_sentence_numbers': target_numbers,
                  'reading_context_sentence_numbers': [i for i, n in enumerate(visible, 1) if n not in owned],
                  'frozen_source_pdf_sha256': PDF_HASH}
        case = {'id': target_id, 'title': title, 'review': review, 'page': original['target_page_start'],
                'source': ' '.join(text for n, text in visible.items() if n in owned)}
        sample.append((case, target))
    return sample


def saved_baseline(output, reference, model, cases):
    """Reuse a completed, unmodified comparison with the same frozen source and model."""
    for path in sorted(output.parent.glob('*/run.json')):
        if path.parent == output:
            continue
        saved = json.loads(path.read_text(encoding='utf-8'))
        if (saved.get('status') != 'completed' or not saved.get('comparison') or saved.get('lookup_policy')
                or saved.get('pdf_sha256') != PDF_HASH or saved.get('model') != model or not saved.get('thinking')
                or saved.get('catalog_signature') != reference.signature or saved.get('baseline_completed') != len(cases)):
            continue
        prior_cases = json.loads((path.parent / 'sample.json').read_text(encoding='utf-8'))
        if prior_cases == cases:
            rows = read_jsonl(path.parent / 'two_stage_claims.jsonl')
            if len(rows) == saved.get('baseline_claim_count'):
                return saved['id'], rows
    return None


def run_sample(pdf, reference, extractor, grounder, output, progress, budget, *, lookup_dir=None, model=None):
    sample = prepare_sample(pdf, with_abbreviations=lookup_dir is not None)
    cases, targets = map(list, zip(*sample))
    save_json(output / 'sample.json', cases)
    save_rows(output / 'targets.jsonl', targets)
    progress(baseline_completed=0)
    prior = saved_baseline(output, reference, model, cases) if lookup_dir is not None else None
    if prior is not None:
        prior_id, baseline = prior
        save_rows(output / 'two_stage_claims.jsonl', baseline)
        progress(baseline_completed=len(cases), baseline_claim_count=len(baseline), baseline_run_id=prior_id)
    run_single_stage(pdf, reference, extractor, output, progress, budget, prepared_targets=targets, lookup_dir=lookup_dir)
    if prior is not None:
        return

    baseline = []
    for index, target in enumerate(targets, 1):
        check_cancelled()
        target = {**target, 'source_abbreviation_definitions': []}
        folder = output / 'two_stage' / target['target_id']
        packet_records = []
        candidates, _, rejections = extract_claims(
            pdf, extractor, 0, lambda *_: None, packets=[target], bibliography={},
            extraction_mode='target_context', prepared_targets=True, packet_results=packet_records,
            scope_instructions=reference.extraction_instructions(),
        )
        save_rows(folder / 'candidates.jsonl', candidates)
        save_rows(folder / 'extraction_rejections.jsonl', rejections)
        if any(record['status'] != 'completed' for record in packet_records):
            raise ValueError(f'Two-stage extraction did not complete for passage {index}.')
        grounded = ground_claims(candidates, folder, reference, grounder, source_packets=[target],
                                 include_definitions=False, workers=1) if candidates else []
        for row in grounded:
            claim = row['original_claim']
            baseline.append({
                **{role: claim[role] for role in ROLES},
                'id': row['candidate_id'], 'target_id': target['target_id'],
                'bindings': {role: {field: row['bindings'][role].get(field)
                                    for field in ('iri', 'label', 'mapping')} for role in ROLES},
                'qualifiers': claim.get('qualifiers', {}), 'scope': row['scope'],
                'classified': classification_complete(row, reference),
                'review': row['extraction_review_status'],
            })
        save_rows(output / 'two_stage_claims.jsonl', baseline)
        progress(baseline_completed=index, baseline_claim_count=len(baseline))
