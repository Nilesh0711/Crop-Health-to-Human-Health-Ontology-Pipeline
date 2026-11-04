"""Offline multi-PDF layout comparison; observable quality signals, not ground truth."""
from pathlib import Path
from pypdf import PdfReader
from backend.shared.storage import file_hash
from backend.stages.pdf_parsing.structure import inspect_pdf
from backend.stages.pdf_parsing.layout import PARSING_POLICY, LEGACY_PARSING_POLICY
from backend.stages.pdf_parsing.packets import pdf_bibliography


def compare_layouts(paths):
    result = []
    for filename in paths:
        path = Path(filename)
        before = file_hash(path)
        reader = PdfReader(str(path))
        modes = {}
        for policy in (LEGACY_PARSING_POLICY,PARSING_POLICY):
            inspection = inspect_pdf(reader,0,policy)
            entries = {r['citation_text']:r for r in pdf_bibliography(path,inspection).values()}
            modes[policy] = {'references':len(entries),'references_with_doi':sum(bool(r.get('doi')) for r in entries.values()),
                'pages':[{k:p.get(k) for k in ('page','word_count','reading_order','layout')} for p in inspection['pages']],
                'pages_with_text':inspection['pages_with_text']}
        if file_hash(path)!=before:
            raise ValueError('Source PDF changed during layout comparison')
        result.append({'pdf':path.name,'sha256':before,'modes':modes})
    return {'documents':result,'scientific_accuracy_measured':False,
            'limitations':['Reference counts and column signals require visual/human verification; more entries do not prove better parsing.']}
