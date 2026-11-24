"""Caption context for cited tables and the citation check for qualifier scope values."""
from backend.domain.extraction_review import extraction_warnings
from backend.stages.claim_extraction.targeting import reading_context_numbers, targeted_prompt


def _packet(texts, target):
    sentences = [{'text': t, 'page': 1, 'section_heading': '3 Results', 'chapter_id': 'c1',
                  'paragraph_id': f'p{i // 3}'} for i, t in enumerate(texts)]
    return {'sentences': sentences, 'target_sentence_numbers': target, 'section_heading': '3 Results',
            'chapter_id': 'c1', 'target_id': 't', 'packet_number': 1}


FILLER = [f'Unrelated sentence number {i} about genotypes and breeding.' for i in range(20)]


def test_table_caption_is_supplied_when_the_target_cites_that_table():
    texts = (['Year analysis for one site was done in 2019 and 2020.',
              'Market groups differed in mineral content (Table 2).',
              'Reds had higher P than russets.'] + FILLER
             + ['TABLE 2 Averages by market group in three environments (Site A 2019 and 2020, Site B 2020).'])
    packet = _packet(texts, [2, 3])
    caption = len(texts)
    assert caption in reading_context_numbers(packet)
    assert f'[{caption}]' in targeted_prompt(packet, 'Paper')


def test_supplementary_references_and_other_tables_do_not_pull_in_captions():
    texts = (['Group differences are in Supplementary Table 3 .', 'Reds had higher P than russets.'] + FILLER
             + ['TABLE 3 Something unrelated.', 'Supplementary Table 3 caption elsewhere.'])
    packet = _packet(texts, [1, 2])
    assert not {len(texts) - 1, len(texts)} & set(reading_context_numbers(packet))


def _claim(cited, condition, subject='Reds', obj='phosphorus'):
    fragments = [{'sentence_number': n, 'text': t} for n, t in cited.items()]
    return {'subject': subject, 'relation': 'had higher', 'object': obj,
            'qualifiers': {'crop_or_species': 'potato', 'condition': condition},
            'source_evidence_sentence_numbers': sorted(cited), 'source_evidence_fragments': fragments,
            'source_evidence_target_fragments': fragments,
            'source_evidence_span': ' '.join(cited.values()),
            'source_reading_context': '[10] [page 5] Analysis of two sites in 2020 found site effects.\n'
                                      '[11] [page 5] Data from Dalhart in 2019 and 2020 had no year effect.'}


def _codes(claim):
    return {w['code'] for w in extraction_warnings(claim)}


def test_year_and_site_from_an_uncited_neighbour_are_flagged_with_the_sentence_number():
    claim = _claim({12: 'Reds had higher P than russets.'}, 'tuber; Dalhart; 2019 and 2020')
    warning = next(w for w in extraction_warnings(claim) if w['code'] == 'qualifier_context_not_cited')
    assert '2019 (sentence 11)' in warning['message'] and 'Dalhart (sentence 11)' in warning['message']


def test_citing_the_sentence_that_establishes_the_value_clears_the_warning():
    claim = _claim({11: 'Data from Dalhart in 2019 and 2020 had no year effect.',
                    12: 'Reds had higher P than russets.'}, 'tuber; Dalhart; 2019 and 2020')
    assert 'qualifier_context_not_cited' not in _codes(claim)


def test_year_found_nowhere_is_reported_separately():
    claim = _claim({12: 'Reds had higher P than russets.'}, 'tuber; 2031')
    assert 'qualifier_year_not_in_source_context' in _codes(claim)


def test_a_word_that_is_part_of_the_endpoint_is_not_a_scope_value():
    claim = _claim({12: 'Plants under low P had less growth.'}, 'low Phosphorus versus high Phosphorus',
                   subject='Phosphorus deficiency', obj='growth')
    claim['source_reading_context'] = '[3] [page 1] Phosphorus is a nutrient.'
    assert not {c for c in _codes(claim) if c.startswith('qualifier_')}


def test_caption_placed_in_the_next_context_packet_is_appended_without_renumbering():
    from backend.stages.claim_extraction.targeting import build_extraction_targets
    texts = ['Year analysis for one site was done in 2019 and 2020.',
             'Market groups differed in mineral content (Table 2).',
             'Reds had higher P than russets.']
    first = {**_packet(texts, [1, 2, 3]), 'packet_number': 1, 'page_start': 1, 'page_end': 1, 'word_count': 30}
    second = {**_packet(['Next paragraph.', 'TABLE 2 Averages in three environments (Site A 2019 and 2020, Site B 2020).'], [1]),
              'packet_number': 2, 'page_start': 1, 'page_end': 1, 'word_count': 20}
    for packet in (first, second):
        for index, sentence in enumerate(packet['sentences']):
            sentence['page'] = 1
    targets = build_extraction_targets([first, second], target_words=200, abbreviation_glossary=[])
    target = targets[0]
    assert target['target_sentence_numbers'] == [1, 2, 3]
    caption = len(target['sentences'])
    assert caption == 4 and caption in target['reading_context_sentence_numbers']
    assert target['sentences'][-1]['appended_context']['origin_packet_number'] == 2
    assert len(first['sentences']) == 3          # the saved packet itself is untouched
    assert f'[{caption}]' in targeted_prompt(target, 'Paper')


def test_cited_abbreviation_expanded_from_uncited_definition_does_not_trigger_uncited_qualifier():
    claim = _claim({12: 'Using NHANES 2003–2006 data, potatoes contributed phosphorus.'},
                   'National Health and Nutrition Examination Survey 2003–2006',
                   subject='daily intake', obj='phosphorus')
    claim['source_reading_context'] = (
        '[10] [page 5] Analysis used data from the National Health and Nutrition Examination Survey (NHANES).'
    )
    claim['source_name_corrections'] = [{
        'field': 'qualifiers.condition',
        'before': 'NHANES 2003–2006',
        'after': 'National Health and Nutrition Examination Survey 2003–2006',
        'short_form': 'NHANES',
        'full_form': 'National Health and Nutrition Examination Survey',
    }]
    assert 'qualifier_context_not_cited' not in _codes(claim)
