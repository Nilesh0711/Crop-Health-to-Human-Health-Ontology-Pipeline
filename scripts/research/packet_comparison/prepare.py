"""Freeze one parsed corpus for both extraction layouts before making API calls."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from pypdf import PdfReader
from backend.shared.storage import file_hash
from backend.stages.pdf_parsing.structure import inspect_pdf
from backend.stages.pdf_parsing.packet_experiments import source_stream,build_packets,coverage_report
from scripts.research.packet_comparison.compare import dump


def prepare(report,pdf_root,corpus,targets):
    if (report/'manifest.json').exists() or list(report.glob('*.prepared.json')):
        raise ValueError('Preparation is immutable; use a fresh report directory')
    names=set()
    prepared=[]
    for item in corpus:
        case_id=item['id']
        if not case_id.replace('_','').isalnum() or case_id in names:
            raise ValueError('Corpus IDs must be unique simple identifiers')
        names.add(case_id)
        pdf=(pdf_root/item['filename']).resolve()
        start,end=item.get('page_start',1),item.get('page_end',0)
        if start<1 or end and end<start:
            raise ValueError('Invalid PDF page interval')
        inspection=inspect_pdf(PdfReader(str(pdf)),end)
        rows=source_stream(pdf,inspection,start,end or None)
        if not rows:
            raise ValueError(f'No parsed source sentences in {case_id}')
        case=dict(id=case_id,pdf=str(pdf),page_start=start,page_end=end or inspection['selected_pages'],
                  stream=rows,inspection=inspection,arms={})
        for arm,options in [('current_4000',{}),('small_2250',dict(target_words=2250,preserve_paragraphs=True,context_sentences=2))]:
            packets=build_packets(rows,**options)
            coverage=coverage_report(rows,packets)
            if coverage['missing_sentence_ids'] or coverage['unknown_sentence_ids'] or coverage['chapter_crossings']:
                raise ValueError(f'Layout coverage failed for {case_id}/{arm}')
            case['arms'][arm]=packets
        prepared.append(case)
    sources={case['id']:{s['source_sentence_id']:s['text'] for s in case['stream']} for case in prepared}
    for target in targets:
        if sources.get(target['case'],{}).get(target['sentence_id'])!=target['quote']:
            raise ValueError('A fixed reference target does not match this parsed source corpus')
    for case in prepared:
        dump(report/f"{case['id']}.prepared.json",case)
    dump(report/'pdf_fingerprints.json',{item['id']:file_hash((pdf_root/item['filename']).resolve()) for item in corpus})
    dump(report/'corpus.json',corpus)
    dump(report/'reference_targets.json',targets)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--pdf-root',type=Path,required=True)
    parser.add_argument('--corpus',type=Path,required=True,help='JSON array of id, filename, page_start, page_end')
    parser.add_argument('--targets',type=Path,required=True,help='Predeclared source-text targets, including restrictions and negative controls')
    args=parser.parse_args()
    prepare(args.report,args.pdf_root,json.loads(args.corpus.read_text(encoding='utf-8')),
            json.loads(args.targets.read_text(encoding='utf-8')))
