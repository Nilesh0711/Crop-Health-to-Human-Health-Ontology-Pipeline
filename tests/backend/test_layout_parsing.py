"""Synthetic columns, lossless fallback and bibliography ambiguity diagnostics."""
from types import SimpleNamespace
from unittest.mock import patch

from backend.stages.pdf_parsing.layout import page_text, PARSING_POLICY, LEGACY_PARSING_POLICY
from backend.stages.literature_retrieval.clients import resolve_bibliographic_reference, openalex_pdf_locations
from backend.config.settings import Settings
from backend.shared.cache import RunCache
from backend.stages.literature_retrieval.workflow import _paper_material


class TwoColumns:
    mediabox=SimpleNamespace(width=600,height=800)
    def extract_text(self,visitor_text=None):
        chunks=[(x,720-i*30,f'{column}{i} reference text.') for i in range(10) for x,column in [(50,'L'),(330,'R')]]
        if visitor_text:
            for x,y,text in chunks:
                visitor_text(text,[1,0,0,1,0,0],[1,0,0,1,x,y],{},10)
        return '\n'.join(text for _,_,text in chunks)


def test_clear_columns_are_ordered_separately_with_all_characters_preserved():
    text,report=page_text(TwoColumns())
    assert text.index('L9')<text.index('R0')
    assert report['columns_detected'] and report['text_preserved']
    assert report['reading_order_verified'] is False
    legacy,_=page_text(TwoColumns(),LEGACY_PARSING_POLICY)
    assert legacy.index('R0')<legacy.index('L1')


def test_position_loss_returns_original_text_instead_of_dropping_evidence():
    class Incomplete(TwoColumns):
        def extract_text(self,visitor_text=None):
            value=super().extract_text(visitor_text)
            return value+' Missing visible text.'
    original=Incomplete().extract_text()
    text,report=page_text(Incomplete(),PARSING_POLICY)
    assert text==original
    assert report['method']=='plain'
    assert report['reason']=='positioned_text_does_not_preserve_all_characters'


def test_already_column_ordered_text_is_preserved_without_coordinate_reconstruction():
    class Ordered(TwoColumns):
        def extract_text(self,visitor_text=None):
            chunks=[(x,720-i*30,f'{column}{i} reference text.') for x,column in [(50,'L'),(330,'R')] for i in range(10)]
            if visitor_text:
                for x,y,text in chunks:
                    visitor_text(text,[1,0,0,1,0,0],[1,0,0,1,x,y],{},10)
            return '\n'.join(t for _,_,t in chunks)
    original=Ordered().extract_text();text,report=page_text(Ordered())
    assert text==original and report['method']=='plain'
    assert report['reason']=='no_clear_column_interleaving'


def test_font_fragments_on_one_line_do_not_split_an_identifier():
    class FontSwitches(TwoColumns):
        def extract_text(self,visitor_text=None):
            original=super().extract_text(visitor_text)
            if visitor_text:
                for x,text in [(50,'10.1234/'),(100,'example')]:
                    visitor_text(text,[1,0,0,1,0,0],[1,0,0,1,x,390],{},10)
                for x,text in [(50,'Written'),(100,'words')]:
                    visitor_text(text,[1,0,0,1,0,0],[1,0,0,1,x,360],{},10)
            return original+'\n10.1234/example\nWritten words'
    text,report=page_text(FontSwitches())
    assert report['columns_detected']
    assert '10.1234/example' in text
    assert 'Written words' in text


def test_reference_diagnostics_distinguish_ambiguity_from_an_empty_reference():
    details={}
    assert resolve_bibliographic_reference({},Settings(),diagnostics=details) is None
    assert details['reason']=='missing_reference_text'
    item={'DOI':'10.1234/a','title':['Compound T modifies Process P'],
          'author':[{'family':'Author'}],'published-print':{'date-parts':[[2020]]}}
    with patch('backend.stages.literature_retrieval.clients.fetch_json',return_value={'message':{'items':[item,{**item,'DOI':'10.1234/b'}]}}):
        assert resolve_bibliographic_reference({'citation_text':'Author 2020 Compound T modifies Process P'},Settings(),details) is None
    assert details['reason']=='ambiguous_reference_match'


def test_open_access_candidates_include_distinct_public_copies_with_a_bound():
    work={'best_oa_location':{'pdf_url':'https://example.org/first.pdf'},
          'locations':[{'is_oa':True,'pdf_url':f'https://example.org/{n}.pdf'} for n in range(8)]}
    assert len(openalex_pdf_locations(work))==4
    assert openalex_pdf_locations(work)[0]=='https://example.org/first.pdf'


def test_second_public_copy_is_attempted_before_abstract_fallback():
    result={'_open_access_pdf_urls':['https://example.org/bad.pdf','https://example.org/good.pdf'],
            'abstractText':'Abstract information. '*10}
    with patch('backend.stages.literature_retrieval.workflow.fetch_open_access_pdf_pages',
               side_effect=[OSError('Unavailable'),[{'page':1,'text':'Literal full text. ' * 30}]]) as fetch:
        paragraphs,access=_paper_material(result,[],Settings(),RunCache())
    assert access=='full_text' and paragraphs
    assert fetch.call_count==2
    assert [d['status'] for d in result['_access_diagnostics']]==['failed','available']


def test_running_headers_are_filtered_without_breaking_body_sentences():
    from backend.stages.pdf_parsing.constants import RUNNING_HEADER_RE
    headers = [
        '318 Savary S. et al.',
        'Savary S. et al. 318',
        'Food Security (2017) 9:315-328',
        'Agronomy 2020, 10, 123 of 456',
        'Plant Pathology (2019) 68, 100-110',
    ]
    for header in headers:
        assert RUNNING_HEADER_RE.match(header), f"Expected {header} to match RUNNING_HEADER_RE"
    body_sentences = [
        'The ability of a number of Fusarium species to produce mycotoxins has made FHB a key factor.',
        'In Ireland, Phytophthora infestans destroyed the potato crop altogether.',
        'In 1845 late blight occurred in some parts of the United States.',
    ]
    for sentence in body_sentences:
        assert not RUNNING_HEADER_RE.match(sentence), f"Body sentence should not match: {sentence}"


class FontDict(dict):
    def get_object(self):
        return self


def _micro_page(fragments):
    """Single-column page: (text, is_micro_font) fragments; plain text keeps the extracted 'm'."""
    micro = FontDict({'/FontDescriptor': FontDict({'/CharSet': '/m'})})
    body = FontDict({'/FontDescriptor': FontDict({'/CharSet': '/m/g/a/b'})})

    class Page:
        mediabox = SimpleNamespace(width=600, height=800)
        def extract_text(self, visitor_text=None):
            if visitor_text:
                for i, (text, is_micro) in enumerate(fragments):
                    visitor_text(text, [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, 50 + i * 40, 700], micro if is_micro else body, 10)
            return ''.join(text for text, _ in fragments)
    return Page()


def test_micro_sign_in_a_unicode_less_symbol_font_is_restored_only_where_proven():
    page = _micro_page([('Macrominerals mg/g and micro in terms of', False), (' m', True), ('g/g on a tuber dry weight basis.', False)])
    text, report = page_text(page, PARSING_POLICY)
    assert 'Macrominerals mg/g' in text          # ordinary body 'm' stays milligram
    assert 'terms of \u00b5g/g on a tuber' in text
    assert report['micro_sign_repaired'] == 1


def test_symbol_glyph_not_followed_by_a_unit_is_left_unchanged_and_reported():
    page = _micro_page([('value ', False), ('m', True), ('ixed model', False)])
    text, report = page_text(page, PARSING_POLICY)
    assert text == 'value mixed model'
    assert report['micro_sign_unrepaired'] == 1 and report['micro_sign_reason'] == 'symbol_glyph_not_followed_by_unit'


def test_previous_policy_reproduces_old_text_for_reading_saved_artifacts():
    from backend.stages.pdf_parsing.layout import PREVIOUS_PARSING_POLICY
    page = _micro_page([('terms of', False), (' m', True), ('g/g on', False)])
    assert page_text(page, PREVIOUS_PARSING_POLICY)[0] == 'terms of mg/g on'


def test_pandey_methods_and_results_keep_microgram_units_in_real_pdf():
    from pathlib import Path
    from pypdf import PdfReader
    pdf = Path('pdf/Pandey_2023_Potato_Tuber_Minerals_and_Phosphorus.pdf')
    if not pdf.exists():
        import pytest
        pytest.skip('Pandey PDF is not part of this checkout')
    reader = PdfReader(str(pdf))
    methods = ' '.join(page_text(reader.pages[2])[0].split())
    results = ' '.join(page_text(reader.pages[4])[0].split())
    assert 'Macrominerals were expressed in terms of mg/g' in methods
    assert 'microminerals were expressed in terms of \u00b5g/g on a tuber dry' in methods
    assert 'higher (8.7%) in Dalhart (20.68 \u00b5g/g) than in Springlake (19.02 \u00b5g/g)' in results
    assert 'Dalhart (3.99 mg/g) than in Springlake (2.93 mg/' in results   # phosphorus is a macromineral
