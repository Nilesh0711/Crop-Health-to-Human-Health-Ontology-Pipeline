"""Coverage and evidence isolation are prerequisites to a packet-size trial."""
from copy import deepcopy

import pytest

from backend.stages.pdf_parsing.packet_experiments import build_packets, coverage_report


def sentences(paragraphs, chapter='chapter-1', heading='Exposure'):
    rows=[]
    for paragraph, texts in enumerate(paragraphs):
        for text in texts:
            rows.append(dict(text=text,page=1,section_heading=heading,chapter_id=chapter,
                             paragraph_id=f'{chapter}-{paragraph}',source_sentence_id=f'{chapter}-{len(rows)}'))
    return rows


def test_smaller_packets_keep_source_text_exact_and_every_sentence():
    rows=sentences([[f'Dose {i} causes a specific outcome only under treatment.' for i in range(6)],
                    ['It does not establish the same effect in untreated plants.']*3,
                    ['High levels of lead and cadmium in soils reduce microbial activity.']])
    original=deepcopy(rows)
    packets=build_packets(rows,35,10,preserve_paragraphs=True,context_sentences=2,paragraph_slack_words=15)
    report=coverage_report(rows,packets)
    assert not report['missing_sentence_ids'] and not report['unknown_sentence_ids']
    assert rows==original
    text={s['source_sentence_id']:s['text'] for s in rows}
    assert all(s['text']==text[s['source_sentence_id']] for p in packets for s in p['sentences'])


def test_context_and_overlap_do_not_cross_chapters():
    rows=sentences([['Lead affects the organism under treatment.']*5])
    rows+=sentences([['In another chapter cadmium has a different outcome.']*5],chapter='chapter-2')
    packets=build_packets(rows,20,8,preserve_paragraphs=True,context_sentences=2,paragraph_slack_words=12)
    assert coverage_report(rows,packets)['chapter_crossings']==0
    assert not coverage_report(rows,packets)['missing_sentence_ids']


def test_context_extension_never_imports_a_different_section():
    rows=sentences([['The dose is high and the treatment is limited.','It reduces microbial activity in that setting.']])
    other=sentences([['A different compound increases microbial activity in another setting.']],heading='Other exposure')
    other[0]['source_sentence_id']='other-section'
    other[0]['paragraph_id']='other-paragraph'
    packets=build_packets(rows+other,10,0,context_sentences=2)
    first=packets[0]
    assert len(first['sentences'])==2
    assert 'It reduces' in first['sentences'][1]['text']
    assert 'other-section' not in {s['source_sentence_id'] for s in first['sentences']}


def test_long_paragraph_has_bounded_extension_and_makes_progress():
    rows=sentences([['A sentence with exactly seven source words here.']*100])
    packets=build_packets(rows,30,10,preserve_paragraphs=True,context_sentences=0,paragraph_slack_words=10)
    assert len(packets)<len(rows)
    assert max(p['word_count'] for p in packets)<=40
    assert not coverage_report(rows,packets)['missing_sentence_ids']


def test_sentence_longer_than_target_is_preserved_as_one_unit():
    rows=sentences([[' '.join(['scientific']*100)],['The next sentence contains another supported outcome.']])
    packets=build_packets(rows,20,5,preserve_paragraphs=True,context_sentences=0)
    assert packets[0]['sentences'][0]['text']==rows[0]['text']
    assert len(packets)==2


def test_paragraph_boundary_preference_keeps_local_referent_with_result():
    rows=sentences([['Control samples received no cadmium exposure.'],
                    ['Treated samples received a high soil cadmium dose.','They showed reduced microbial activity compared with controls.'],
                    ['Other experiments used a different soil type.']])
    packets=build_packets(rows,17,5,preserve_paragraphs=True,context_sentences=0,paragraph_slack_words=20)
    assert rows[1]['text'] in [s['text'] for s in packets[0]['sentences']]
    assert rows[2]['text'] in [s['text'] for s in packets[0]['sentences']]


@pytest.mark.parametrize('options',[dict(target_words=0),dict(target_words=10,overlap_words=10),dict(context_sentences=-1),dict(paragraph_slack_words=-1)])
def test_invalid_layout_fails_before_model_call(options):
    with pytest.raises(ValueError):
        build_packets([],**options)


def test_empty_source_has_no_fabricated_packet():
    assert build_packets([])==[]
