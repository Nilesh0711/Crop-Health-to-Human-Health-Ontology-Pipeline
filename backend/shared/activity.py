"""Format request activity and display saved diagnostics without rewriting logs."""
import re


STAGE_ALIASES = {'extraction': 'claim_extraction', 'source': 'source_validation',
                 'query': 'query_planning', 'external': 'external_validation'}


class TaggedMessage(str):
    """Preserve two-argument emit callbacks while carrying an explicit stage."""
    def __new__(cls, message, stage, candidate_id=None):
        value = super().__new__(cls, message)
        value.stage = STAGE_ALIASES.get(stage, stage)
        value.candidate_id = candidate_id
        return value


def activity_stage(entry, default='system'):
    explicit = entry.get('stage') or (entry.get('progress') or {}).get('stage')
    if explicit:
        return STAGE_ALIASES.get(explicit, explicit)
    text = str(entry.get('message') or '').casefold()
    patterns = (
        ('pathway_assembly', ('pathway assembly', 'assembling path', 'registry')),
        ('chunk_preparation', ('ranked ', 'passage preparation', 'chunk preparation')),
        ('article_fetch', ('fetching paper', 'paper fetched')),
        ('article_ranking', ('ranking article', 'metadata ranking')),
        ('literature_search', ('europe pmc queries', 'literature search', 'resolving citation')),
        ('query_planning', ('query planning', 'query translation', 'search planning', 'llm query')),
        ('source_validation', ('source evidence', 'source judgement', 'source fidelity', 'source validation', 'source batch', 'source model', 'llm source')),
        ('claim_extraction', ('extracting ', 'extraction ', 'extraction:', 'llm extraction')),
        ('schema_validation', ('controlled vocabulary', 'schema validation')),
        ('pdf_parsing', ('pdf text', 'parsed pdf', 'document scope', 'text quality')),
        ('graph_construction', ('building knowledge graph', 'graph building')),
        ('external_validation', ('joint external', 'paper external', 'cross-paper synthesis', 'external validation', 'validating ', 'claim status:', 'claim ', 'external model', 'llm external')),
    )
    return next((stage for stage, phrases in patterns if any(phrase in text for phrase in phrases)), default)


def research_activity(entry):
    """Keep research milestones/outcomes and problems; omit routine diagnostics."""
    level = entry.get('level')
    if level == 'progress':
        return False  # Live request state is persisted separately, not as log rows.
    if level in {'error', 'warning', 'stage'}:
        return True
    text = str(entry.get('message') or '').casefold()
    if 'recovery completed successfully' in text:
        return True
    if activity_stage(entry) in {'literature_search', 'article_ranking', 'article_fetch', 'chunk_preparation'}:
        return False
    routine = (
        r'^fetching(?: and preparing)? \d+ papers', r'^ranking articles:',
        r'^ranked \d+ passages:', r'^passage preparation completed:',
        r'^europe pmc queries:', r'^literature search returned', r'^retrieval lookahead:',
        r'^opencode .+ request #\d+: ok;',
        r'^llm ', r'^extraction mode:', r'^document scope:', r'^standalone semantic model',
        r'^loading (?:semantic reranker|external contradiction checker)',
        r'^(?:semantic reranker|external contradiction checker) ready',
        r'^(?:source|query) batch \d+:',
        r'^extracting .+ \(pages ', r'accepted candidates so far$',
        r'^external validation: paper \d+/', r'^resuming retained evidence packet',
        r'^pathway assembly new/changed endpoints',
    )
    return text != 'external validation' and not any(re.search(pattern, text) for pattern in routine)


_OLD_PARSER_ERROR = re.compile(r'\b[A-Za-z][\w.-]* returned non-JSON output:')
_MODEL_CONTEXT = re.compile(r'(?:\bmodel\s+|\bOpenCode\s+)(opencode-go/[\w.-]+)')
_CHARACTER_COUNT = re.compile(r'\(\d+ prompt characters\)$')


def input_token_label(record):
    """Show provider input usage only; character lengths cannot establish tokens."""
    count = record.get('input_tokens')
    reported = record.get('input_tokens_reported', record.get('usage_reported', False))
    if reported and type(count) is int and count >= 0:
        return f'{count:,} input tokens (provider-reported)'
    if record.get('status') in {'waiting_for_provider', 'repairing_format', 'regenerating_response'}:
        return 'input tokens pending provider response'
    return 'input tokens unavailable'


def progress_message(progress):
    return f"{progress.get('task')}: {progress['status']} ({input_token_label(progress)})"


def display_error(message):
    if not isinstance(message, str):
        return message
    return _OLD_PARSER_ERROR.sub('Response JSON parsing failed:', message)


def display_log(entry):
    """Only presentation changes; callers retain the original stored record."""
    result = dict(entry)
    result['stage'] = activity_stage(entry)
    original = result.get('message')
    result['message'] = display_error(original)
    changed = original != result['message']
    progress = entry.get('progress')
    model = progress.get('model') if isinstance(progress, dict) else None
    if isinstance(progress, dict):
        progress = dict(progress)
        if isinstance(result['message'], str) and _CHARACTER_COUNT.search(result['message']):
            result['message'] = _CHARACTER_COUNT.sub('(' + input_token_label(progress) + ')', result['message'])
            result['original_message'] = original
        status = progress.get('provider_status')
        corrected = display_error(status)
        if corrected != status:
            progress['provider_status'] = corrected
            progress['original_provider_status'] = status
            changed = True
        result['progress'] = progress
    if changed:
        match = _MODEL_CONTEXT.search(str(original or '') + ' ' + str((progress or {}).get('provider_status', '')))
        model = model or (match.group(1) if match else None)
        notice = 'Historical parser label corrected for display; original log preserved.'
        if model:
            notice += ' Recorded model: ' + model + '.'
        result.update(original_message=original, historical_error_notice=notice)
        if isinstance(progress, dict):
            progress['historical_error_notice'] = notice
    return result


def display_request(request):
    result = dict(request)
    original = result.get('error')
    result['error'] = display_error(original)
    if original != result['error']:
        result['original_error'] = original
        result['historical_error_notice'] = 'Historical parser wording; model attribution comes from the recorded model field.'
    return result
