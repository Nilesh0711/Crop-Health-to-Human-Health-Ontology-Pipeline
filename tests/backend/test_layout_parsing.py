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
