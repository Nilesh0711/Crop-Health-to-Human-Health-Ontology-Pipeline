"""Coverage-aware article and chunk ranking for neighboring claim links and their local connection."""
from backend.stages.evidence_ranking.articles import _hybrid_order


def balanced_articles(candidate, eligible, reranker):
    if not eligible:
        return []
    orders = []
    for target in candidate['ranking_targets']:
        proxy = {**candidate, **{k: target[k] for k in ('subject', 'object', 'relation', 'qualifiers', 'subject_type', 'object_type') if k in target}}
        ranked = _hybrid_order(target['text'], proxy, eligible, reranker)
        for n, (_, (result, _, _)) in enumerate(ranked, 1):
            result.setdefault('_pathway_target_ranks', {})[target['target_id']] = n
        orders.append(ranked)
    # Reserve candidates for each retrieval facet, not a single topic score.
    result, seen = [], set()
    for rank in range(len(eligible)):
        for rows in orders:
            if rank < len(rows) and id(rows[rank][1][0]) not in seen:
                seen.add(id(rows[rank][1][0])); result.append(rows[rank])
    return result


def balanced_passages(candidate, chunks, article_key, budget, reranker):
    targets = candidate['ranking_targets']
    queries = [(t['text'],t.get('neutral_text') or ' '.join(str(t.get(k) or '') for k in ('subject','object')).strip() or t['text']) for t in targets]
    orders = (reranker.select_many(queries,chunks,article_key,budget) if hasattr(reranker,'select_many') else
              [reranker.select(text,chunks,article_key,budget,neutral_query=neutral) for text,neutral in queries])
    selected, by_id = [], {}
    # A paper can contain several non-adjacent relevant findings. Keep exact
    # chunk identity/text and annotate every target selecting the same chunk.
    for rank in range(budget):
        for target, rows in zip(targets, orders):
            if rank >= len(rows):
                continue
            chunk = rows[rank]
            previous = by_id.get(chunk['chunk_id'])
            if previous:
                if previous['text'] != chunk['text']:
                    raise ValueError('One paper/chunk identity has conflicting source text')
                previous['retrieval_target_ids'] = sorted(set(previous['retrieval_target_ids']) | {target['target_id']})
            elif len(selected) < budget:
                kept = {**chunk, 'retrieval_target_ids': [target['target_id']]}
                selected.append(kept); by_id[chunk['chunk_id']] = kept
    return selected


