"""Version and provenance shared by production and offline readers."""
POLICY = 'scoped-index-hybrid-rrf-v10'
DATABASE = 'scoped_fts5.sqlite3'
FORMULAS = 'chebi_formulas.json'
HASHES = {
    DATABASE: 'ef09c613b18a829fcf63cb1798ed86a1739396acb4cb7a41a73221f3a03a4951',
    FORMULAS: 'a7cec87b7018aaf710d20045a99b0a8f1b9f729453ca1c207827d6fd84ed0139',
}
SIGNATURE = ':'.join([POLICY, *HASHES.values()])


def inventory_stale(row):
    # Legacy full-catalogue packets remain available to isolated evaluation callers.
    return (row.get('class_selection_policy') in {
                POLICY, 'scoped-index-hybrid-rrf-v9', 'scoped-index-hybrid-rrf-v8', 'scoped-index-hybrid-rrf-v7',
                'scoped-index-lexical-typed-shortlists-v6',
                'scoped-index-lexical-typed-shortlists-v5',
                'scoped-index-lexical-typed-shortlists-v4',
                'scoped-index-core-and-relation-shortlists-v3',
                'scoped-index-core-and-relation-shortlists-v2', 'scoped-formula-exact-bm25-v1'}
            or bool(row.get('inventory_signature'))) and row.get('inventory_signature') != SIGNATURE


def retrieved_class(binding):
    term = binding.get('retrieved_term') or {}
    return bool(term.get('kind') == 'class' and term.get('iri') == binding.get('iri')
                and term.get('inventory_signature') == SIGNATURE)
