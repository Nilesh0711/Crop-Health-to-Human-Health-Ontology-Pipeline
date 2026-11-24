"""Conservative review signals, never automatic scientific corrections."""
import re

from backend.shared.storage import stable_hash
from backend.shared.text import normalized, source_surface_mention
from backend.stages.claim_extraction.contract import MEASURED_OBSERVATION_CONTRACT_VERSION
from backend.ontology.usage import direct_property_allowed
from backend.ontology.inventory_policy import retrieved_class
from backend.ontology.property_types import (
    BFO_INDEPENDENT_CONTINUANT,
    BFO_MATERIAL_ENTITY,
    BFO_OCCURRENT,
    BFO_PROCESS,
    BFO_QUALITY,
    BFO_SDC,
    GO_MOLECULAR_FUNCTION,
    PATO_CHARACTERISTIC,
    check_property_types,
    class_ancestors,
)

REVIEW_POLICY = 'separate-extraction-and-mapping-review-v12'
ELIGIBILITY_POLICY = 'grounded-predicate-eligibility-v7'
POSITIVE = re.compile(r'\b(?:increas\w*|enhanc\w*|promot\w*|improv\w*|absor\w*|acquir\w*|caus\w*|reduc\w*|decreas\w*)\b', re.I)
NEGATIVE = re.compile(r'\b(?:cannot|can\s+not|could\s+not|does\s+not|did\s+not|no\s+significant\s+(?:effect|difference|increase|decrease)|not\s+(?:statistically\s+)?significant|no\s+(?:such\s+)?(?:relationship|correlation|association)s?|not\s+considerable)\b', re.I)
RESTRICTIVE = re.compile(r'\bonly\s+(?:\d+|one|two|three|four|five)\s+(?:out\s+of|of)\s+(?:\d+|\w+)\b', re.I)
LIMITATION = re.compile(r'\b(?:limited\s+availability\s+of|scarcity\s+of|deficiency\s+of|lack\s+of|low\s+availability\s+of)\s+', re.I)
SIGNIFICANCE = re.compile(r'\bsignificant(?:ly)?\b', re.I)
NON_SIGNIFICANCE = re.compile(
    r'\b(?:no (?:statistically )?significant|not (?:statistically )?significant|'
    r'(?:did|do|does|was|were) not [^.;]{0,100}\bsignificant(?:ly)?|non[- ]significant)\b', re.I)
_COMPARISON_CUES = {
    'higher': re.compile(r'\b(?:higher|greater|more|larger|longer|increas\w*|rose)\b', re.I),
    'lower': re.compile(r'\b(?:lower|fewer|less|smaller|shorter|decreas\w*|declin\w*|fell|reduced)\b', re.I),
    'unchanged': re.compile(r'\b(?:unchanged|remained (?:constant|stable|the same)|did not change)\b', re.I),
    'no_significant_difference': NON_SIGNIFICANCE,
}
_OBSERVATION_CAUSAL_RE = re.compile(r'\b(?:caus\w*|regulat\w*|induc\w*|leads? to|led to|result\w* in)\b', re.I)


def _comparison_issue(claim, fragments):
    comparison = claim['measured_comparison']
    cue = ' '.join(closed_relation_source_cue(part['text'], claim.get('relation_trigger') or '') for part in fragments)
    qualifiers = claim['qualifiers']
    baseline = comparison['comparator_quote']
    comparator = qualifiers.get('comparator', '')
    if _OBSERVATION_CAUSAL_RE.search(claim['relation']) or _CLOSED_HEDGE_RE.search(cue):
        return 'The source must report a measured comparison and the wording must preserve that observation.'
    if bool(baseline) != bool(comparator) or (baseline and (
        not source_surface_mention(claim['source_evidence_span'], baseline)
        or not source_surface_mention(baseline, comparator)
        or normalized(baseline) == normalized(claim.get('subject_source_mention') or claim['subject'])
    )):
        return 'Check that the source-stated baseline is quoted and kept in the comparator qualifier.'
    direction = comparison['direction']
    if not _COMPARISON_CUES[direction].search(cue) or (NON_SIGNIFICANCE.search(cue) and direction == 'unchanged'):
        return 'The quoted comparison cue does not establish the reported direction.'
    significance = ('not_significant' if NON_SIGNIFICANCE.search(cue)
                    else 'significant' if SIGNIFICANCE.search(cue) else 'not_reported')
    if comparison['statistical_significance'] != significance:
        return 'Check that the reported statistical significance matches the quoted comparison.'
    return None


def _change_direction(text):
    if NEGATIVE.search(text):
        return None
    up = bool(re.search(r'\b(?:increas\w*|rose|ris\w*)\b', text, re.I))
    down = bool(re.search(r'\b(?:declin\w*|decreas\w*|fell|fall\w*)\b', text, re.I))
    return ('up' if up else 'down') if up != down else None


def _stated_trend(text, endpoint):
    # Only a clause ending in the observed change (optionally a percentage).
    # "Factor T increased Trait R" does not say Factor T itself increased.
    change = re.search(r'\b(?:increased|decreased|declined|rose|fell)\b'
                       r'(?:\s+by\s+[<>≤≥~]?\d+(?:\.\d+)?\s*%)?\s*[.!?]?\s*$', text, re.I)
    if change and source_surface_mention(text[:change.start()], endpoint):
        return _change_direction(change[0])
    return None


def _role_signals(claim, text):
    """Narrow clause-local diagnostics, not a dependency parser or a correction."""
    subject = claim.get('subject_source_mention') or claim.get('subject', '')
    obj = claim.get('object_source_mention') or claim.get('object', '')
    relation = str(claim.get('relation', ''))
    location = re.search(r'\b(higher|lower)\s+in\b', relation, re.I)
    if location:
        for compared in re.finditer(r'\bthan\b', text, re.I):
            prefix, suffix = text[:compared.start()], ' '.join(text[compared.end():].split()[:14])
            if (source_surface_mention(prefix, subject) and not source_surface_mention(prefix, obj)
                    and source_surface_mention(suffix, obj)
                    and re.search(r'\b' + location[1] + r'\b', prefix, re.I)):
                yield 'possible_comparison_role_mismatch', 'The compared group may have become the bearer of the higher/lower trait; review the roles.'
                break
    # Both original endpoints must be located on opposite sides of a stated
    # trend. An unrelated increase/negative word elsewhere cannot trigger this.
    clauses = re.split(r'[,;]|\b(?:as|while|whereas)\b', text, flags=re.I)
    for left in clauses:
        if not source_surface_mention(left, subject) or source_surface_mention(left, obj):
            continue
        for right in clauses:
            if (left == right or not source_surface_mention(right, obj)
                    or source_surface_mention(right, subject)):
                continue
            direct = _stated_trend(left, subject)
            if direct and _stated_trend(right, obj) not in (None, direct):
                # "A declined as B increased" states A's change; an active
                # "B increased A" instead records an effect on A. Compare
                # the recorded change with its grammatical recipient, never
                # simply with the first variable mentioned in the sentence.
                parts = re.split(r'\b(?:with|as|while)\b', relation, flags=re.I)
                recorded = _change_direction(parts[0])
                expected = direct if len(parts) > 1 else _stated_trend(right, obj)
                if recorded and recorded != expected:
                    yield 'possible_role_inversion', 'The recorded trend may reverse which source variable changed; review the clause roles.'
                    break
    tails = list(re.finditer(r'[,;]\s*(?:thereby\s+)?(enhanc|facilitat|promot|support|result|lead)\w*\b', text, re.I))
    if tails:
        # A coordinated later result must follow an actual result clause.
        # Ordinary "has Trait Q and enhanced Trait R" is not a mechanism tail.
        first_result = tails[0].start()
        tails.extend(m for m in re.finditer(r'\band\s+(enhanc|facilitat|promot|support|result|lead)\w*\b', text, re.I)
                     if m.start() > first_result)
    for tail in tails:
        prefix = text[:tail.start()]
        subject_span = source_surface_mention(prefix, subject)
        # Locate an intervening source-stated action or bearer description.
        # Do not depend on the model repeating its subject in population, and
        # do not flag a bare subject followed by an ordinary descriptive clause.
        if not subject_span:
            continue
        after_subject = prefix[prefix.casefold().find(subject_span.casefold()) + len(subject_span):]
        mechanism = re.search(r"\b(?:ability|capacity|allocat\w*|exhibit\w*|show\w*|demonstrat\w*|managed|has|had|have)\b", after_subject, re.I)
        if (mechanism and len(after_subject.split()) >= 4
                and source_surface_mention(text[tail.end():], obj)
                and re.search(r'\b' + tail[1] + r'\w*\b', relation, re.I)):
            yield 'possible_mechanism_substitution', 'A population may replace the preceding mechanism in a downstream outcome clause; review the subject.'
            break
    s_norm = ' '.join(str(claim.get('subject', '')).split()).casefold()
    o_norm = ' '.join(str(claim.get('object', '')).split()).casefold()
    if s_norm and o_norm and s_norm != o_norm:
        short, long_ep = (s_norm, o_norm) if len(s_norm) < len(o_norm) else (o_norm, s_norm)
        if (len(short.split()) >= 2 and long_ep.startswith(short)
                and re.match(r'^\s+(?:of|for|under)\s+\S+', long_ep[len(short):])
                and re.search(r'\b(?:lessen\w*|decreas\w*|declin\w*|increas\w*|rose|ris\w*|fell|fall\w*|diminish\w*|drop\w*|reduc\w*)\b', relation, re.I)
                and not re.search(r'\b(?:than|compared|versus|vs\.?|relative to)\b', relation, re.I)):
            long_span = source_surface_mention(text, long_ep)
            if long_span and not source_surface_mention(text.replace(long_span, ' ', 1), short):
                yield ('possible_self_referential_endpoint',
                       'Subject and object refer to the same source trait mention with a dropped group modifier in a unary change statement; review the clause roles.')
    if _COMP_TRAIT_IN_REL.search(relation.strip()):
        yield ('possible_comparison_trait_in_predicate',
               'The measured trait appears inside a comparative predicate between two groups; review whether the trait belongs in an endpoint and the comparison group in comparator.')
    # Active consumption verbs require the consumer in subject rather than the consumed substance.
    cons_m = _ACTIVE_CONSUMPTION_RE.search(relation)
    if cons_m and not re.search(r'\b(?:consumed|ingested|taken)\s+by\b', relation, re.I):
        text_cons = re.search(rf'\b{re.escape(cons_m[0][:5])}\w*\b(.{{0,140}})', text, re.I)
        if text_cons:
            of_m = re.search(r'\b(?:amount|amounts|dose|doses|quantity|quantities|level|levels|intake|intakes)\s+of\b(.{1,120})', text_cons[1], re.I)
            before_verb = text[:text_cons.start()]
            if of_m and source_surface_mention(of_m[1], subject) and not source_surface_mention(before_verb, subject):
                yield ('possible_agent_role_reversal',
                       'The consumed substance appears in subject of an active consumption verb; review agent and substance roles.')
    # "addition of <Additive> is [not] permitted in [the case of] <Food>" must not make Food added to Additive.
    if re.search(r'\b(?:added|applied|supplied)\s+to\b', relation, re.I):
        add_m = re.search(r'\b(?:addition|application|supply)\s+of\s+(.{1,120}?)\s+(?:is|are|was|were)\b(.{1,140})', text, re.I)
        if add_m and source_surface_mention(add_m[1], obj) and source_surface_mention(add_m[2], subject):
            yield ('possible_agent_role_reversal',
                   'The recipient food or matrix appears in subject as if added to the additive; review clause roles.')
    alias = str(claim.get('predicate_alias') or '')
    if alias in _FORWARD_DIRECTIONAL_ALIASES:
        if claim.get('passive_role_reversal_verified') is False:
            yield ('possible_passive_role_reversal_failure',
                   'Passive source clause may not be reversed into forward subject -> object order; review clause roles.')
        elif claim.get('passive_role_reversal_verified') is not True:
            pass_m = _PASSIVE_BY_RE.search(text)
            if (pass_m and source_surface_mention(text[:pass_m.start()], subject)
                    and source_surface_mention(text[pass_m.end():], obj)):
                yield ('possible_passive_role_reversal_failure',
                       'Passive source clause places patient before the passive verb and agent after by/with; forward property requires reversing passive roles.')
        if _has_reversed_active_clause(text, subject, obj, claim.get('relation_trigger') or relation):
            yield ('possible_agent_role_reversal',
                   'Active clause order places object before the relation cue and subject after it; review agent and recipient roles.')


_FORWARD_DIRECTIONAL_ALIASES = frozenset({'P1', 'P2', 'P3', 'P4', 'P6', 'P7', 'P8', 'P9'})
_PASSIVE_BY_RE = re.compile(
    r'\b(?:was|were|is|are|been|be|being)\s+(?:[A-Za-z-]+\s+){0,3}[A-Za-z-]+(?:ed|en)\s+(?:by|with)\b',
    re.I,
)


def _has_reversed_active_clause(clause, subject_span, object_span, predicate=''):
    if not (clause and subject_span and object_span) or _PASSIVE_BY_RE.search(clause):
        return False
    s_span = source_surface_mention(clause, subject_span)
    o_span = source_surface_mention(clause, object_span)
    if not (s_span and o_span):
        return False
    s_hit = re.search(rf'(?<!\w){re.escape(s_span)}(?!\w)', clause, re.I)
    o_hit = re.search(rf'(?<!\w){re.escape(o_span)}(?!\w)', clause, re.I)
    if not (s_hit and o_hit) or o_hit.end() >= s_hit.start():
        return False
    between = clause[o_hit.end():s_hit.start()]
    if ',' in between or re.search(r'\b(?:of|from|between|in|under|at|during|upon|with)\s*$', between.strip(), re.I):
        return False
    pred_tokens = {t[:5] for t in re.findall(r'[a-z0-9]+', str(predicate).casefold()) if len(t) >= 4}
    between_tokens = {t[:5] for t in re.findall(r'[a-z0-9]+', between.casefold()) if len(t) >= 4}
    return bool((pred_tokens & between_tokens) or re.search(r'\b(?:cannot|can|could|did|does|do|will|may|might)\b', between, re.I))


_BUNDLED_ORGAN_RE = re.compile(
    r'\b(?:both\s+)?(?:roots?|shoots?|leaves|leaf|stems?|tubers?)\s+and\s+(?:roots?|shoots?|leaves|leaf|stems?|tubers?)\b',
    re.I,
)
_BUNDLED_COMMA_AND_RE = re.compile(r',[^,]+,?\s+and\s+', re.I)
_DISTINCT_TRAIT_TOKEN = (
    r'(?:root\s+length|root\s+surface\s+area|root\s+volume|root\s+diameter|'
    r'root\s+biomass|shoot\s+biomass|root\s+efficiency|specific\s+(?:p|phosphorus)\s+uptake|'
    r'total\s+(?:p|phosphorus)\s+uptake|(?:p|phosphorus)\s+concentration|(?:p|phosphorus)\s+content|'
    r'pue|pupe|pute|pae|phosphorus\s+(?:use|uptake|utilization|acquisition)\s+efficiency)'
)
_BUNDLED_TRAIT_AND_RE = re.compile(rf'\b{_DISTINCT_TRAIT_TOKEN}\b\s+and\s+\b{_DISTINCT_TRAIT_TOKEN}\b', re.I)
_RATIO_OR_TRANSPORT_RE = re.compile(r'(?:\bratio\b|\broot-to-shoot\b|\bshoot-to-root\b|\b(?:from|between)\b.+\b(?:to|and)\b)', re.I)
_JOINT_TREATMENT_RE = re.compile(r'\b(?:combined|co-inoculation|mixture|interaction)\b', re.I)

_CLOSED_ANOVA_RE = re.compile(r'\b(?:was|were)\s+(?:significantly\s+)?(?:affected|influenced)\s+by\s+(?:[A-Za-z-]+\s+){0,3}levels?\b', re.I)
_CLOSED_NEUTRAL_AFFECT_RE = re.compile(r'\b(?:affected|influenced)\b', re.I)
_CLOSED_COMP_RE = re.compile(
    r'\b(?:higher|lower|greater|smaller|more|less)\s+than\b|\bcompared\s+(?:to|with)\b|'
    r'\b(?:surpass\w*|exceed\w*|outperform\w*)\b|'
    r'\b(?:was|were|had|exhibited|showed|displayed|saw|observed|found|recorded)\s+'
    r'(?:[<>≤≥~]?\d+(?:\.\d+)?(?:\s*[–−-]\s*\d+(?:\.\d+)?)?\s*%\s+)?(?:higher|lower|greater|smaller)\b',
    re.I,
)
_CLOSED_TREND_REL_RE = re.compile(r'\b(?:declin\w*|increas\w*|decreas\w*|rose|ris\w*|fell|fall\w*)\s+as\b', re.I)
_CLOSED_PARALLEL_TEXT_RE = re.compile(
    r'(?:\bas\s+.{1,80}\b(?:increas\w*|decreas\w*|declin\w*|rose|ris\w*|fell|fall\w*)\b|'
    r'\b(?:rose|ris\w*|fell|fall\w*|declin\w*|increas\w*|decreas\w*)\s+as\s+.{1,80}\b(?:increas\w*|decreas\w*|declin\w*|rose|ris\w*|fell|fall\w*))',
    re.I,
)
_CLOSED_CAUSAL_VERB_RE = re.compile(r'\b(?:caus\w*|induc\w*|led\s+to|result(?:s|ed)?\s+in|promot\w*|enhanc\w*|improv\w*|reduc\w*)\b', re.I)
_CLOSED_OBSERVATION_CUE_RE = re.compile(r'\b(?:observed|recorded|measured|noted)\b', re.I)
_FINDING_CLAUSE_BOUNDARY_RE = re.compile(r';|,\s*(?=(?:but|whereas|while|which|suggesting|indicating)\b)', re.I)
_CONDITION_PREFIX_RE = re.compile(r'\b(?:under|in|at|during)\s+(?:(?:a|an|the)\s+)?$', re.I)
_WITH_PREFIX_RE = re.compile(r'\bwith\s+(?:(?:a|an|the)\s+)?$', re.I)
_MATERIAL_TRANSFER_CUE_RE = re.compile(
    r'\b(?:deliver\w*|transfer\w*|transport\w*|translocat\w*|absorb\w*|absorption|uptake|accumulat\w*)\b', re.I,
)
_MATERIAL_PRODUCTION_CUE_RE = re.compile(
    r'\b(?:produc\w*|biosynthesi\w*|synthesi\w*|manufactur\w*|generat\w*)\b', re.I,
)
_CLOSED_HEDGE_RE = re.compile(r'\b(?:can|may|might|could|potentially|possibly|believed\s+to)\b', re.I)
_CLOSED_NEGATION_RE = re.compile(r"\b(?:no|not|never|cannot|(?:ca|could|did|does|was|were)n['’]t)\b", re.I)
_CLOSED_POS_CUE_RE = re.compile(r'\b(?:increas\w*|improv\w*|enhanc\w*|elevat\w*|promot\w*|augment\w*|amplif\w*|stimulat\w*|positively|positive\s+effect|rose|ris\w*|more|additional)\b', re.I)
_CLOSED_NEG_CUE_RE = re.compile(r'\b(?:decreas\w*|reduc\w*|declin\w*|diminish\w*|inhibit\w*|suppress\w*|lower\w*|lessen\w*|negatively|negative\s+effect|fell)\b', re.I)
_CLOSED_CUE_PREFIX_RE = re.compile(
    r"\b(?:(?:can|may|might|could|cannot|(?:ca|could|did|does|was|were)n['’]t|"
    r'no|not|never|was|were|is|are|be|been|being|have|has|had|do|does|did|also|'
    r'significant|strong|weak|positive|negative|[a-z]+ly)\s+)+$', re.I,
)


_PROCESS_DOMAIN_ALIASES = frozenset({'P1', 'P2', 'P4', 'P8', 'P9', 'P11', 'P12'})
_PROCESS_SUBJECT_CUE_RE = re.compile(
    r'\b(?:process\w*|pathway\w*|treatment\w*|intervention\w*|inoculat\w*|co-inoculat\w*|'
    r'applicat\w*|appl(?:y|ied|ies|ying)|suppl(?:y|ied|ies|ying|ement\w*)|fertiliz\w*|addition|adding|'
    r'expos\w*|incubat\w*|irrigat\w*|cultivat\w*|grow(?:th|ing)|develop\w*|'
    r'increas\w*|decreas\w*|increment\w*|elevat\w*|reduc\w*|declin\w*|deplet\w*|'
    r'deficien\w*|scarcit\w*|starv\w*|stress\w*|limitation\w*|deprivation|shortage|excess|availabilit\w*|insufficienc\w*|'
    r'accumulat\w*|uptak\w*|acquisit\w*|translocat\w*|transport\w*|allocat\w*|'
    r'mobiliz\w*|immobiliz\w*|metaboliz\w*|mineraliz\w*|solubiliz\w*|coloniz\w*|infect\w*|'
    r'mutat\w*|overexpress\w*|silenc\w*|knockout|absorpt\w*|exudat\w*|secretion|'
    r'synthesi\w*|biosynthesi\w*|assimilat\w*|utiliz\w*|activit\w*|reaction\w*|respon\w*|'
    r'adaptat\w*|erosion|removal|runoff|leaching|management|handling|explor\w*|'
    r'branching|formation|expansion|modificat\w*|alterat\w*|produc\w*|consum\w*|'
    r'express\w*|sequest\w*|partition\w*|modulat\w*|recycl\w*|remodel\w*|excret\w*|retent\w*|calcif\w*|load(?:ing)?|reabsorpt\w*|homeosta\w*|'
    r'ingest\w*|intak\w*|cook\w*|boil\w*|fry\w*|bak\w*|stor(?:age|ing)|digestion|'
    r'amend\w*|water\w*|spray\w*|harvest\w*|sowing|seeding|tilling|tillage|mulch\w*|'
    r'stimulat\w*|enhanc\w*|improv\w*|promot\w*|inhibit\w*|suppress\w*|condition\w*|regime\w*|shift\w*|change\w*|mechanism\w*)\b',
    re.I,
)
_FINDING_COMPARATOR_RE = re.compile(
    r'\b(?:(?:more|less)\s+[a-z]+ly\s+than|compared\s+(?:to|with)|relative\s+to)\s+'
    r'((?:the\s+)?[A-Za-z0-9][\w.-]*(?:\s+[A-Za-z0-9][\w.-]*){0,3})',
    re.I,
)
_COMPARATOR_STOPWORDS = frozenset({'the', 'a', 'an', 'in', 'of', 'to', 'for', 'those', 'that'})


def closed_relation_source_cue(text, trigger):
    """Include modifiers immediately before a quoted cue for validation."""
    span = source_surface_mention(text, trigger)
    if not span:
        return ''
    prefix = _CLOSED_CUE_PREFIX_RE.search(text[:text.index(span)])
    return prefix[0] + span if prefix else span


def _condition_as_causal_subject(text, cue, subject, obj):
    before, found, after = text.partition(cue)
    if not found:
        return False
    finding = _FINDING_CLAUSE_BOUNDARY_RE.split(before)[-1] + cue + _FINDING_CLAUSE_BOUNDARY_RE.split(after)[0]
    subject_span = source_surface_mention(finding, subject)
    if not subject_span or not source_surface_mention(finding, obj):
        return False
    # The verbatim cue may already contain either endpoint. Check source roles
    # in the finding itself, independently of the model's cue length.
    mentions = list(re.finditer(rf'(?<!\w){re.escape(subject_span)}(?!\w)', finding, re.I))
    observed = _CLOSED_OBSERVATION_CUE_RE.search(finding)
    return bool(mentions) and all(
        _CONDITION_PREFIX_RE.search(finding[:m.start()])
        or (observed and _WITH_PREFIX_RE.search(finding[:m.start()]))
        for m in mentions
    )


def _active_finding_text(claim, text, trigger, obj):
    fragments = claim.get('source_evidence_fragments') or []
    obj_mention = str(claim.get('object_source_mention') or obj or '').strip()
    matched = [
        str(f.get('text') or '') for f in fragments
        if str(f.get('text') or '')
        and (not trigger or source_surface_mention(str(f.get('text') or ''), trigger))
        and (not obj_mention or source_surface_mention(str(f.get('text') or ''), obj_mention))
    ]
    if not matched and fragments and trigger:
        matched = [
            str(f.get('text') or '') for f in fragments
            if str(f.get('text') or '') and source_surface_mention(str(f.get('text') or ''), trigger)
        ]
    return ' '.join(matched) if matched else text


def _closed_relation_signals(claim, text):
    alias = str(claim.get('predicate_alias') or '')
    if not alias:
        return
    for role in ('subject', 'object'):
        val = str(claim.get(role) or '').strip()
        if not val or _RATIO_OR_TRANSPORT_RE.search(val):
            continue
        if role == 'subject' and _JOINT_TREATMENT_RE.search(val):
            continue
        if _BUNDLED_ORGAN_RE.search(val) or _BUNDLED_COMMA_AND_RE.search(val) or _BUNDLED_TRAIT_AND_RE.search(val):
            yield ('possible_bundled_outcomes',
                   f'The {role} endpoint bundles multiple organs or traits instead of one atomic concept; split before admitting.')
            break
    if alias not in {'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7', 'P8', 'P9', 'P10', 'P11', 'P12'}:
        return
    relation = str(claim.get('relation') or '').strip()
    trigger = str(claim.get('relation_trigger') or '').strip()
    cue = closed_relation_source_cue(text, trigger)
    if trigger and not cue:
        return
    rel_text = f'{relation} {cue}'.strip()
    subj = str(claim.get('subject') or '')
    obj = str(claim.get('object') or '')
    if _CLOSED_ANOVA_RE.search(rel_text) or (
        _CLOSED_NEUTRAL_AFFECT_RE.search(cue or relation) and trigger
        and source_surface_mention(text, trigger) and _CLOSED_ANOVA_RE.search(text)
    ):
        yield ('possible_unsupported_closed_relation',
               'Undirected factor-level statistical significance does not establish an approved causal or directional relation.')
        return
    if alias in {'P1', 'P2', 'P3', 'P4', 'P10', 'P11', 'P12'} and cue and _condition_as_causal_subject(
        text, cue, claim.get('subject_source_mention') or subj, claim.get('object_source_mention') or obj,
    ):
        yield ('possible_unsupported_closed_relation',
               'An observed change under a condition does not state that the condition caused the change.')
        return
    if _CLOSED_COMP_RE.search(rel_text):
        yield ('possible_unsupported_closed_relation',
               'Comparative wording between groups/levels is outside approved closed-relation predicates P1-P12.')
        return
    if _CLOSED_TREND_REL_RE.search(rel_text) or (
        alias in {'P1', 'P2', 'P3', 'P4', 'P10', 'P11', 'P12'} and _CLOSED_PARALLEL_TEXT_RE.search(text)
        and source_surface_mention(text, subj) and source_surface_mention(text, obj)
        and not _CLOSED_CAUSAL_VERB_RE.search(text)
    ):
        yield ('possible_unsupported_closed_relation',
               'Parallel trends without explicit causal attribution are outside approved causal predicates.')
        return
    if _CLOSED_HEDGE_RE.search(rel_text) or _CLOSED_NEGATION_RE.search(rel_text) or NEGATIVE.search(rel_text):
        yield ('possible_unsupported_closed_relation',
               'Hedged, negated, or non-detected wording cannot carry an affirmed closed-relation predicate.')
        return
    if alias == 'P6' and _MATERIAL_TRANSFER_CUE_RE.search(cue) and not _MATERIAL_PRODUCTION_CUE_RE.search(cue):
        yield ('possible_unsupported_closed_relation',
               'Movement, uptake or buildup of existing material does not assert that the subject produced it.')
        return
    if alias in {'P1', 'P11'} and (_CLOSED_NEG_CUE_RE.search(rel_text) or not _CLOSED_POS_CUE_RE.search(cue or relation)):
        lbl = 'positively regulates characteristic' if alias == 'P1' else 'positively regulates'
        yield ('possible_unsupported_closed_relation',
               f'{alias} ({lbl}) requires an unambiguous increase/positive cue.')
        return
    if alias in {'P2', 'P12'} and (_CLOSED_POS_CUE_RE.search(rel_text) or not _CLOSED_NEG_CUE_RE.search(cue or relation)):
        lbl = 'negatively regulates characteristic' if alias == 'P2' else 'negatively regulates'
        yield ('possible_unsupported_closed_relation',
               f'{alias} ({lbl}) requires an unambiguous decrease/negative cue.')
        return
    if (alias in _PROCESS_DOMAIN_ALIASES
            and claim.get('passive_role_reversal_verified') is not False
            and not _has_reversed_active_clause(text, subj, obj, trigger or relation)
            and not _PROCESS_SUBJECT_CUE_RE.search(subj)):
        yield ('possible_non_process_subject',
               'The selected relation requires a process or experimental intervention in subject, but subject names a bare organism or material entity without an established intervention process.')
    if alias in {'P1', 'P2', 'P3', 'P4', 'P10', 'P11', 'P12'} and (not trigger or source_surface_mention(text, trigger)):
        finding_text = _active_finding_text(claim, text, trigger, obj)
        comp_m = _FINDING_COMPARATOR_RE.search(finding_text)
        if comp_m:
            comp_qual = str((claim.get('qualifiers') or {}).get('comparator') or '').strip()
            comp_tokens = [
                tok.rstrip('.,;:)') for tok in comp_m.group(1).split()
                if tok.rstrip('.,;:)') and tok.rstrip('.,;:)').casefold() not in _COMPARATOR_STOPWORDS and len(tok.rstrip('.,;:)')) >= 3
            ]
            if not comp_qual or (comp_tokens and not any(source_surface_mention(comp_qual, tok) for tok in comp_tokens)):
                yield ('possible_dropped_comparator',
                       'The cited finding sentence states an explicit comparison group (e.g. more ... than / compared to), but qualifiers.comparator is missing it.')


_NUMBER = r'[<>≤≥~≈]?\s*[+−-]?\d+(?:\.\d+)?(?:\s*(?:–|—|-|to)\s*[+−-]?\d+(?:\.\d+)?)?'
_UNIT = r'(?:%|[munµμk]?g|[munµμk]?l|[munµμk]?m|mol|mmol|µmol|μmol|ppm|ppb|°c|k|pa|mpa|s|h)'
_CHANGE_NOUN = r'(?:\s+(?:increase|decrease|reduction|decline|drop|rise|gain|loss|change|difference)s?)?'
_QUANTITY = re.compile(rf'{_NUMBER}(?:\s*{_UNIT}(?:\s*[−-]?\s*\d+)?)?(?:\s*[/·]?\s*{_UNIT}\s*[−-]?\s*\d*)*{_CHANGE_NOUN}', re.I)
_COMP_TRAIT_IN_REL = re.compile(
    r'\b(?:more|greater|higher|lower|less|larger|smaller|longer|shorter|wider|narrower|better|worse|enhanced|reduced|diminished|heightened)\b'
    r'\s+(?:[\w-]+\s+){1,10}(?:compared\s+(?:to|with)|than|versus|vs\.?)\s*$',
    re.I,
)
_ACTIVE_CONSUMPTION_RE = re.compile(r'\b(?:consumes?|ingests?|eats?|drinks?|takes?\s+in)\b', re.I)
_COMPOSITIONAL_RELATION_IRIS = {
    'http://purl.obolibrary.org/obo/BFO_0000051',
    'http://purl.obolibrary.org/obo/BFO_0000050',
    'http://purl.obolibrary.org/obo/RO_0017005',
}
_REGULATORY_LIMIT_RE = re.compile(
    r'\b(?:can\s+contain\s+up\s+to|may\s+contain\s+up\s+to|is\s+permitted\s+up\s+to|'
    r'not\s+permitted|in\s+the\s+sense\s+of\s+regulation|regulation\s*\(?\s*ec|'
    r'maximum\s+(?:permitted\s+)?(?:level|limit)|permitted\s+(?:maximum|limit)|acceptable\s+daily\s+intake)\b',
    re.I,
)
_TIME = re.compile(rf'{_NUMBER}\s*(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)'
    rf'(?:\s+(?:after|before|post)\s+[\w -]+)?(?:\s+to\s+{_NUMBER}\s*'
    r'(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)(?:\s+(?:after|before|post)\s+[\w -]+)?)?', re.I)
_TIME_UNIT_ONLY = re.compile(
    r'(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)(?:\s+(?:after|before|post)\s+[\w -]+)?',
    re.I,
)
_TEMPORAL_CHANGE = re.compile(r'\b(?:increas\w*|decreas\w*|declin\w*|rose|ris\w*|fell|fall\w*|chang\w*|lessen\w*|reduc\w*|from\s+\d+)\b', re.I)


def _endpoint_quantity_kind(value):
    """Only whole pure spans, never embedded numbers in chemicals/traits/ratios."""
    value = str(value).strip()
    if _TIME.fullmatch(value):
        return 'temporal'
    if _QUANTITY.fullmatch(value):
        return 'measurement'
    return None


_MINERAL_TRAIT_RE = re.compile(
    r'\b(?:phosphorus|nitrogen|potassium|calcium|magnesium|sulfur|zinc|iron|copper|manganese|boron|sodium|'
    r'macromineral\w*|micromineral\w*|mineral\w*)\s+(?:content\w*|concentration\w*|level\w*)\b'
    r'|\b(?:concentrations?\s+(?:for|of)\b.{0,30}\bminerals?\b)'
    r'|(?:^|\b)(?:phosphorus|potassium|calcium|magnesium|sulfur|zinc|iron|copper|manganese|boron|sodium)\b'
    r'(?=.*?\b(?:higher|lower|highest|lowest|content\w*|concentration\w*|level\w*|interaction\w*|correlat\w*|mg/g|g/kg|µg/g|ug/g)\b)'
    r'|\b(?:higher|lower|highest|lowest|content\w*|concentration\w*|level\w*|interaction\w*|correlat\w*|mg/g|g/kg|µg/g|ug/g)\b'
    r'(?=.*?\b(?:phosphorus|potassium|calcium|magnesium|sulfur|zinc|iron|copper|manganese|boron|sodium|minerals?)\b)',
    re.I,
)
_MINERAL_MEASUREMENT_METRIC_RE = re.compile(r'\b(?:mg/g|g/kg|µg/g|ug/g|ppm)\b', re.I)
_WEIGHT_BASIS_RE = re.compile(r'(?:\b(?:dry|fresh)[-\s]weight\b|\bDW\b|\bFW\b)')
_WEIGHT_BASIS_TEXT_RE = re.compile(r'(?:\b(?:dry|fresh)[-\s]weight\b)', re.I)
_MINERAL_ORGAN_CONTEXT_RE = re.compile(
    r'\b(?:tubers?\s+(?:mineral|phosphorus|dry|sample)|'
    r'(?:mineral|phosphorus|nutrient)\s+(?:content|concentration)s?\s+(?:of|in)\s+(?:raw\s+)?(?:potato\s+)?(?:tubers?|roots?|shoots?|leaves)|'
    r'on\s+a\s+(?:tuber|root|shoot|leaf)\s+(?:dry|fresh)[-\s]weight)\b',
    re.I,
)
_ORGAN_TERM_RE = re.compile(r'\b(?:tubers?|roots?|shoots?|leaves|leaf|foliar)\b', re.I)
_LOCATION_COMPARISON_RE = re.compile(r'\b(?:location|Dalhart|Springlake|site)\b', re.I)
_TRIAL_YEAR_RE = re.compile(r'\b20(?:19|20)\b')
_ANY_YEAR_RE = re.compile(r'\b(?:19|20)\d{2}\b')


def _has_weight_basis(text: str) -> bool:
    return bool(_WEIGHT_BASIS_RE.search(text) or _WEIGHT_BASIS_TEXT_RE.search(text))


def _missing_measurement_context_signals(claim, kept, first_sentence):
    qualifiers = claim.get('qualifiers') or {}
    metric = str(qualifiers.get('metric') or '')
    if not (_MINERAL_TRAIT_RE.search(kept) or _MINERAL_MEASUREMENT_METRIC_RE.search(metric)):
        return []
    span = str(claim.get('source_evidence_span') or first_sentence.get('text') or '')
    local_ctx = str(claim.get('source_local_context') or '')
    reading_ctx = str(claim.get('source_reading_context') or '')
    context_pool = ' '.join(part for part in (span, local_ctx, reading_ctx) if part)
    missing = []
    if _has_weight_basis(context_pool) and not _has_weight_basis(kept):
        missing.append('measurement weight basis (dry/fresh weight)')
    if _MINERAL_ORGAN_CONTEXT_RE.search(context_pool) and not _ORGAN_TERM_RE.search(kept):
        missing.append('measured plant organ/tissue')
    if (qualifiers.get('comparator') and _LOCATION_COMPARISON_RE.search(kept)
            and _TRIAL_YEAR_RE.search(f'{span} {local_ctx}') and not _ANY_YEAR_RE.search(kept)):
        missing.append('comparison trial year')
    if not missing:
        return []
    return [(
        'possible_missing_measurement_context',
        f"Essential source context ({', '.join(missing)}) is present in supplied evidence/context but omitted from the claim; review qualifiers and cited context sentences.",
    )]


_CONTEXT_LINE_RE = re.compile(r'^\[(\d+)\](?:\s*\[page[^\]]*\])?\s*(.+)$', re.M)
_NAMED_SCOPE_RE = re.compile(r'\b[A-Z][a-z]{3,}\b')
_SCOPE_WORDS_IGNORED = frozenset({'table', 'figure', 'supplementary', 'results', 'methods', 'data', 'analysis',
                                  'significant', 'location', 'environment', 'environments', 'market', 'group'})


def _supplied_sentences(claim):
    """Sentence number -> text for every supplied reading/local-context line."""
    sentences = {}
    for field in ('source_reading_context', 'source_local_context'):
        for number, text in _CONTEXT_LINE_RE.findall(str(claim.get(field) or '')):
            sentences.setdefault(int(number), text)
    return sentences


def _uncited_qualifier_context_signals(claim):
    """A year, site or cultivar in condition/population must be established by a *cited* sentence.

    Qualifiers copied from a neighbouring, uncited sentence cannot be source-reviewed from the
    citation list, and may describe a different analysis than the cited finding.
    """
    qualifiers = claim.get('qualifiers') or {}
    cited_numbers = {n for n in (claim.get('source_evidence_sentence_numbers') or []) if type(n) is int}
    cited_text = ' '.join(str(f.get('text') or '') for f in (claim.get('source_evidence_fragments') or []))
    cited_text = f"{cited_text} {claim.get('source_evidence_span') or ''}"
    scope_parts = []
    for key in ('condition', 'population'):
        val = str(qualifiers.get(key) or '')
        field = f'qualifiers.{key}'
        for corr in reversed(claim.get('source_name_corrections') or ()):
            if corr.get('field') == field and val == corr.get('after') and 'before' in corr:
                short = corr.get('short_form') or ''
                if not short or re.search(rf'(?<!\w){re.escape(short)}(?!\w)', cited_text, re.I):
                    val = str(corr['before'])
        scope_parts.append(val)
    scope = ' '.join(scope_parts)
    if not scope.strip():
        return []
    supplied = _supplied_sentences(claim)
    uncited = {n: t for n, t in supplied.items() if n not in cited_numbers}
    years = set(_ANY_YEAR_RE.findall(scope))
    # A word that is already part of the finding's own endpoints (e.g. "Phosphorus" in "Phosphorus
    # deficiency") is the trait itself, not a scope value that needs its own citation.
    endpoints = f"{claim.get('subject') or ''} {claim.get('object') or ''}".casefold()
    names = {w for w in _NAMED_SCOPE_RE.findall(scope)
             if w.casefold() not in _SCOPE_WORDS_IGNORED and w.casefold() not in endpoints}
    found_elsewhere, nowhere = {}, []
    for token in sorted(years | names):
        pattern = re.compile(rf'(?<!\w){re.escape(token)}(?!\w)', re.I)
        if pattern.search(cited_text):
            continue
        homes = sorted(n for n, text in uncited.items() if pattern.search(text))
        if homes:
            found_elsewhere[token] = homes
        elif token in years:
            nowhere.append(token)
    signals = []
    if found_elsewhere:
        detail = '; '.join(f"{token} (sentence {', '.join(map(str, homes[:4]))})" for token, homes in found_elsewhere.items())
        signals.append(('qualifier_context_not_cited',
                        f'Qualifier value(s) appear only in supplied but uncited sentences: {detail}. Cite the sentence that '
                        'establishes them for this finding, or remove the qualifier if it describes a different analysis.'))
    if nowhere:
        signals.append(('qualifier_year_not_in_source_context',
                        f"Qualifier year(s) {', '.join(nowhere)} appear in no cited or supplied sentence; remove or source them."))
    return signals


def extraction_warnings(claim):
    """Sentence-level review signals; no clause splitting and no automatic corrections."""
    warnings = []
    relation = str(claim.get('relation') or '')
    kept = ' '.join(str(claim.get(k) or '') for k in ('subject', 'relation', 'object')) + ' ' + ' '.join(
        str(v) for v in (claim.get('qualifiers') or {}).values() if v)
    fragments = claim.get('source_evidence_target_fragments') or claim.get('source_evidence_fragments') \
        or [{'text': claim.get('source_evidence_span', '')}]
    def add(code, text, sentence):
        if not any(w['code'] == code for w in warnings):
            warnings.append({'code': code, 'message': text, 'sentence_number': sentence.get('sentence_number'),
                             'source_text': sentence['text'], 'policy': REVIEW_POLICY})
    if claim.get('extraction_contract_version') == MEASURED_OBSERVATION_CONTRACT_VERSION and claim.get('measured_comparison'):
        if issue := _comparison_issue(claim, fragments):
            add('possible_unsupported_measured_comparison', issue, fragments[0])
    for sentence in fragments:
        text = sentence['text']
        for role in ('subject', 'object'):
            raw_value = str(claim.get(role, '')).strip()
            kind = _endpoint_quantity_kind(raw_value)
            anchor = claim.get(role + '_source_mention') or claim.get(role, '')
            if (kind is None and _TIME_UNIT_ONLY.fullmatch(raw_value)
                    and _TEMPORAL_CHANGE.search(f'{relation} {text}')
                    and (_TIME.search(text) or _TIME.search(relation)
                         or (anchor and re.search(rf'{_NUMBER}\s+{re.escape(str(anchor).strip())}(?!\w)', text, re.I)))):
                kind = 'temporal'
            if kind and source_surface_mention(text, anchor):
                add('possible_' + kind + '_endpoint',
                    f'The {role} is a pure {kind} span; review whether it belongs in qualifiers rather than as a scientific endpoint.', sentence)
        if re.search(r'\b(?:elevat\w*|increas\w*|decreas\w*|reduc\w*|higher|lower|highest|lowest|peak\w*|maximum|minimum|constant)\b.{0,120}\b(?:to|at|under|beyond)\s*$', relation, re.I):
            original = str(claim.get('object_source_mention') or claim.get('object', '')).strip().casefold()
            for label in claim.get('source_treatment_labels', []):
                if (label.get('label', '').casefold() == original
                        and label.get('document') == claim.get('source_document_id')
                        and label.get('source_pdf_sha256') == claim.get('source_pdf_sha256')
                        and label.get('text_sha256') == stable_hash(label.get('text', ''), 64)
                        and source_surface_mention(text, original)):
                    add('possible_treatment_code_endpoint',
                        'The object is a source-defined experimental label in a change/extremum frame; review whether it belongs in conditions.', sentence)
                    next(w for w in warnings if w['code'] == 'possible_treatment_code_endpoint')['label_provenance'] = label
                    break
        for code, message in _role_signals(claim, text):
            add(code, message, sentence)
        for code, message in _closed_relation_signals(claim, text):
            add(code, message, sentence)
        if NEGATIVE.search(text) and not NEGATIVE.search(kept) and POSITIVE.search(relation):
            add('possible_lost_negation', 'The cited sentence contains a negation or non-significance that the claim does not keep.', sentence)
        if RESTRICTIVE.search(text) and not RESTRICTIVE.search(kept):
            add('possible_generalized_subset', 'The cited sentence restricts the finding to a subset that the claim does not keep.', sentence)
        if LIMITATION.search(text) and not LIMITATION.search(kept) and not re.search(r'\b(?:limited|low|deficien\w*|scarc\w*)\b', kept, re.I):
            add('possible_lost_defining_state', 'The cited sentence names a limitation or deficiency state that the claim does not keep.', sentence)
    if fragments:
        for code, message in _missing_measurement_context_signals(claim, kept, fragments[0]):
            add(code, message, fragments[0])
        for code, message in _uncited_qualifier_context_signals(claim):
            add(code, message, fragments[0])
    return warnings


def review_status(row):
    review = row.get('extraction_review') or {}
    if (row.get('claim_signature') and review.get('claim_signature') == row['claim_signature']
            and review.get('status') in {'accepted', 'rejected'}):
        return review['status']
    return 'held' if row.get('extraction_warnings') else 'not_flagged'


def can_share(row, role):
    if row['original_claim'].get('measured_comparison'):
        return False
    binding = row['bindings'][role]
    qualifiers = row['original_claim'].get('qualifiers') or {}
    warned_roles = {w.get('role') for w in (row.get('mapping_review_warnings') or [])}
    return (review_status(row) not in {'held', 'rejected'} and not row.get('mapping_consistency_warnings')
            and role not in warned_roles
            and binding.get('iri') and binding['mapping'] == 'exact' and binding['denotation'] == 'concept'
            and not row['original_claim'].get(role + '_context')
            and not any(qualifiers.get(k) for k in ('population', 'condition', 'comparator', 'metric')))


_FUNCTION_MEASUREMENT_REL = re.compile(
    r'\b(?:affect\w*|influenc\w*|increas\w*|decreas\w*|diminish\w*|amplif\w*|elevat\w*|reduc\w*'
    r'|declin\w*|higher|lower|highest|lowest|peak\w*|enhanc\w*|suppress\w*|inhibit\w*|stimulat\w*'
    r'|modulat\w*|alter\w*|chang\w*|optimis\w*|optimiz\w*|regul\w*)\b',
    re.I,
)
_FUNCTION_MEASUREMENT_CONTEXT = re.compile(
    r'\b(?:assay\w*|measur\w*|determin\w*|level\w*|rate\w*|activit(?:y|ies)\s+in\b'
    r'|in\s+(?:the\s+)?(?:roots?|shoots?|leaves?|tubers?|stems?|tissues?))\b',
    re.I,
)


def _is_molecular_function_term(reference, ep_iri: str) -> bool:
    terms = getattr(reference, 'terms', {}) or {}
    term = terms.get(ep_iri) or {}
    if GO_MOLECULAR_FUNCTION in class_ancestors(reference, ep_iri):
        return True
    if ep_iri.startswith('http://purl.obolibrary.org/obo/GO_'):
        label = str(term.get('label', '')).lower()
        defn = str(term.get('definition', ''))
        if label.endswith(' activity') or defn.startswith(('Catalysis of ', 'Enables ')):
            return True
    return False


HP_PHENOTYPIC_ABNORMALITY = 'http://purl.obolibrary.org/obo/HP_0000118'
MONDO_DISEASE = 'http://purl.obolibrary.org/obo/MONDO_0000001'
BFO_HAS_PART = 'http://purl.obolibrary.org/obo/BFO_0000051'
BFO_PART_OF = 'http://purl.obolibrary.org/obo/BFO_0000050'
RO_CAUSES_CONDITION = 'http://purl.obolibrary.org/obo/RO_0003302'
CMO_MEASUREMENT = 'http://purl.obolibrary.org/obo/CMO_0000000'
OBA_ATTRIBUTE = 'http://purl.obolibrary.org/obo/OBA_0000001'
_CHEBI_PREFIX = 'http://purl.obolibrary.org/obo/CHEBI_'
_INDEPENDENT_CONTINUANT_IRIS = frozenset({BFO_INDEPENDENT_CONTINUANT, BFO_MATERIAL_ENTITY})
_SDC_QUALITY_IRIS = frozenset({BFO_SDC, BFO_QUALITY, PATO_CHARACTERISTIC})
_OCCURRENT_IRIS = frozenset({BFO_OCCURRENT, BFO_PROCESS})

_ENDPOINT_EXPLICIT_ABNORMALITY_RE = re.compile(
    r'\b(?:'
    r'abnormal\w*|hyper[a-z]{3,}|hypo[a-z]{3,}|nephropath\w*|nephrocalcinos\w*|'
    r'calcification\w*|fibros\w*|deminerali[sz]ation\w*|insufficienc\w*|'
    r'impair\w*|failure\w*|damage\w*|injur\w*|lesion\w*|disease\w*|disorder\w*|'
    r'syndrome\w*|deficien\w*|depletion\w*|necros\w*|sclerosis|stenosis|'
    r'reduced\s+[a-z]+\s+function|decreased\s+[a-z]+\s+density|'
    r'loss\s+of\s+[a-z]+|nephron\s+loss|renal\s+loss'
    r')\b',
    re.I,
)
_HEALTH_ENDPOINT_RE = re.compile(r'\bhealth\b', re.I)


def _endpoint_term_metadata(binding: dict, reference=None) -> tuple[str, str, set[str]]:
    """Resolve label, definition, and verified ancestors from reference catalog or retrieved metadata."""
    ep_iri = binding.get('iri')
    if not ep_iri:
        return '', '', set()
    ref_term = (getattr(reference, 'terms', None) or {}).get(ep_iri) or {}
    ret_term = binding.get('retrieved_term') or {}
    label = str(ref_term.get('label') or ret_term.get('label') or binding.get('label') or '')
    defn = str(ref_term.get('definition') or ret_term.get('definition') or '')
    anc = set(class_ancestors(reference, ep_iri)) if hasattr(reference, 'terms') else set()
    if retrieved_class(binding) or binding.get('verified_ancestor_iris'):
        anc.update(binding.get('verified_ancestor_iris') or ())
    return label, defn, anc


def mapping_meaning_warnings(row, reference=None) -> list[dict]:
    """Flag meaning-changing mappings: regulatory limits, neutral->abnormal HP, polarity inversions, mereology mismatches, and function conflation."""
    claim = row.get('original_claim') or {}
    relation = str(claim.get('relation') or '')
    span = str(claim.get('source_evidence_span') or '')
    qualifiers = claim.get('qualifiers') or {}
    bindings = row.get('bindings') or {}
    warnings: list[dict] = []

    rel_binding = bindings.get('relation') or {}
    rel_iri = rel_binding.get('iri')
    if (rel_binding.get('mapping') in {'exact', 'broader'}
            and rel_iri in _COMPOSITIONAL_RELATION_IRIS
            and _REGULATORY_LIMIT_RE.search(f"{relation} {qualifiers.get('condition') or ''} {span}")):
        rel_label = ((getattr(reference, 'terms', {}) or {}).get(rel_iri) or {}).get('label') or rel_binding.get('label') or rel_iri.rsplit('/', 1)[-1]
        warnings.append({
            'code': 'regulatory_limit_mapped_as_measured_content',
            'role': 'relation',
            'iri': rel_iri,
            'label': rel_label,
            'message': (
                f"relation maps to compositional '{rel_label}', while the claim and source describe a regulatory "
                f"permitted maximum or prohibition rather than measured physical content."
            ),
            'policy': ELIGIBILITY_POLICY,
        })

    # Safeguard 1: neutral measurement mapped to abnormal HP phenotype or health->disease polarity inversion.
    ep_meta: dict[str, tuple[str, str, set[str]]] = {}
    for role in ('subject', 'object'):
        binding = bindings.get(role) or {}
        ep_iri = binding.get('iri')
        mapping = binding.get('mapping') or 'unresolved'
        if not ep_iri or mapping == 'unresolved':
            continue
        label, defn, anc = _endpoint_term_metadata(binding, reference)
        ep_meta[role] = (label, defn, anc)
        disp_label = label or ep_iri.rsplit('/', 1)[-1]
        ep_text = str(claim.get(role) or '')
        if (mapping in {'exact', 'broader'}
                and (HP_PHENOTYPIC_ABNORMALITY in anc or ep_iri.startswith('http://purl.obolibrary.org/obo/HP_'))
                and not _ENDPOINT_EXPLICIT_ABNORMALITY_RE.search(ep_text)):
            warnings.append({
                'code': 'neutral_measurement_mapped_to_abnormal_phenotype',
                'role': role,
                'iri': ep_iri,
                'label': disp_label,
                'message': (
                    f"{role} '{ep_text}' is a neutral or relative measurement mapped ({mapping}) to "
                    f"abnormal phenotype class '{disp_label}' without source-stated abnormality."
                ),
                'policy': ELIGIBILITY_POLICY,
            })
        elif (mapping in {'exact', 'broader', 'narrower'}
              and (MONDO_DISEASE in anc or ep_iri.startswith('http://purl.obolibrary.org/obo/MONDO_')
                   or re.search(r'\bdisease\b', label, re.I))
              and _HEALTH_ENDPOINT_RE.search(ep_text)
              and not _ENDPOINT_EXPLICIT_ABNORMALITY_RE.search(ep_text)):
            warnings.append({
                'code': 'polarity_inversion_health_to_disease',
                'role': role,
                'iri': ep_iri,
                'label': disp_label,
                'message': (
                    f"{role} '{ep_text}' is mapped ({mapping}) to disease class '{disp_label}', "
                    f"inverting health polarity."
                ),
                'policy': ELIGIBILITY_POLICY,
            })

    # Safeguard 2: BFO mereology category mismatch on has_part / part_of.
    if rel_iri in {BFO_HAS_PART, BFO_PART_OF} and rel_binding.get('mapping') in {'exact', 'broader'}:
        subj = bindings.get('subject') or {}
        obj = bindings.get('object') or {}
        if (subj.get('mapping') in {'exact', 'broader'} and subj.get('iri')
                and obj.get('mapping') in {'exact', 'broader'} and obj.get('iri')):
            s_lbl, _, s_anc = ep_meta.get('subject') or _endpoint_term_metadata(subj, reference)
            o_lbl, _, o_anc = ep_meta.get('object') or _endpoint_term_metadata(obj, reference)
            s_ind = bool(s_anc & _INDEPENDENT_CONTINUANT_IRIS) or subj['iri'].startswith(_CHEBI_PREFIX)
            o_ind = bool(o_anc & _INDEPENDENT_CONTINUANT_IRIS) or obj['iri'].startswith(_CHEBI_PREFIX)
            s_sdc = bool(s_anc & _SDC_QUALITY_IRIS)
            o_sdc = bool(o_anc & _SDC_QUALITY_IRIS)
            s_occ = bool(s_anc & _OCCURRENT_IRIS)
            o_occ = bool(o_anc & _OCCURRENT_IRIS)
            rel_label = ((getattr(reference, 'terms', None) or {}).get(rel_iri) or {}).get('label') or rel_binding.get('label') or rel_iri.rsplit('/', 1)[-1]
            s_disp = s_lbl or subj['iri'].rsplit('/', 1)[-1]
            o_disp = o_lbl or obj['iri'].rsplit('/', 1)[-1]
            if (s_ind and o_sdc) or (s_sdc and o_ind):
                warn_role = 'object' if ((rel_iri == BFO_HAS_PART and s_ind and o_sdc)
                                         or (rel_iri == BFO_PART_OF and s_sdc and o_ind)) else 'subject'
                warn_ep = obj if warn_role == 'object' else subj
                warn_lbl = o_disp if warn_role == 'object' else s_disp
                warnings.append({
                    'code': 'parthood_category_mismatch',
                    'role': warn_role,
                    'iri': warn_ep.get('iri'),
                    'label': warn_lbl,
                    'message': (
                        f"Material parthood '{rel_label}' cannot pair an independent/material continuant "
                        f"('{s_disp}') with a quality/characteristic ('{o_disp}')."
                    ),
                    'policy': ELIGIBILITY_POLICY,
                })
            elif (s_occ and (o_ind or o_sdc)) or (o_occ and (s_ind or s_sdc)):
                warnings.append({
                    'code': 'parthood_category_mismatch',
                    'role': 'relation',
                    'iri': rel_iri,
                    'label': rel_label,
                    'message': (
                        f"Parthood '{rel_label}' cannot mix an occurrent/process with a continuant "
                        f"('{s_disp}' vs '{o_disp}')."
                    ),
                    'policy': ELIGIBILITY_POLICY,
                })

    # Safeguard 3: RO:0003302 (causes or contributes to condition) requires a disease/abnormal phenotype condition object, not a neutral measurement.
    if rel_iri == RO_CAUSES_CONDITION and rel_binding.get('mapping') in {'exact', 'broader'}:
        obj = bindings.get('object') or {}
        o_iri = obj.get('iri')
        if obj.get('mapping') in {'exact', 'broader'} and o_iri:
            o_lbl, _, o_anc = ep_meta.get('object') or _endpoint_term_metadata(obj, reference)
            is_condition = (
                HP_PHENOTYPIC_ABNORMALITY in o_anc
                or MONDO_DISEASE in o_anc
                or o_iri.startswith(('http://purl.obolibrary.org/obo/HP_', 'http://purl.obolibrary.org/obo/MONDO_'))
            )
            is_neutral_measurement = (
                CMO_MEASUREMENT in o_anc
                or OBA_ATTRIBUTE in o_anc
                or o_iri.startswith((
                    'http://purl.obolibrary.org/obo/CMO_',
                    'http://purl.obolibrary.org/obo/OBA_',
                    'http://purl.obolibrary.org/obo/TO_',
                ))
            )
            if is_neutral_measurement and not is_condition:
                o_disp = o_lbl or o_iri.rsplit('/', 1)[-1]
                warnings.append({
                    'code': 'condition_property_with_neutral_measurement_object',
                    'role': 'object',
                    'iri': o_iri,
                    'label': o_disp,
                    'message': (
                        f"object '{o_disp}' is a neutral measurement or trait rather than a disease or abnormal phenotype/condition required by 'causes or contributes to condition'."
                    ),
                    'policy': ELIGIBILITY_POLICY,
                })

    if not hasattr(reference, 'terms'):
        existing = list(row.get('mapping_review_warnings') or [])
        seen_codes = {(w.get('code'), w.get('role')) for w in warnings}
        return warnings + [w for w in existing if (w.get('code'), w.get('role')) not in seen_codes]

    has_quant_context = bool(
        _FUNCTION_MEASUREMENT_REL.search(relation)
        or _FUNCTION_MEASUREMENT_CONTEXT.search(span)
        or qualifiers.get('metric')
    )
    for role in ('subject', 'object'):
        binding = bindings.get(role) or {}
        ep_iri = binding.get('iri')
        if binding.get('mapping') != 'exact' or not ep_iri or ep_iri not in reference.terms:
            continue
        if not _is_molecular_function_term(reference, ep_iri):
            continue
        ep_text = str(claim.get(role) or '')
        if has_quant_context or _FUNCTION_MEASUREMENT_CONTEXT.search(ep_text):
            label = (reference.terms.get(ep_iri) or {}).get('label') or binding.get('label') or ep_iri.rsplit('/', 1)[-1]
            warnings.append({
                'code': 'possible_function_vs_measurement_conflation',
                'role': role,
                'iri': ep_iri,
                'label': label,
                'message': (
                    f"{role} maps exact to GO molecular function '{label}', while the claim describes a "
                    f"measured or modulated activity level rather than catalytic function identity."
                ),
                'policy': ELIGIBILITY_POLICY,
            })
    return warnings


def assertion_eligible(row, reference=None):
    """Endpoint classification alone never admits an unresolved predicate."""
    if ((row.get('original_claim') or {}).get('measured_comparison')
            or any((b.get('components') or {}).get('grounding_completeness') in {'held', 'partial'} for b in row.get('bindings', {}).values())):
        return False
    relation = row.get('bindings', {}).get('relation', {})
    if relation.get('under_review'):
        return False
    pt = check_property_types(row, reference) if reference is not None else (row.get('property_type_check') or {})
    meaning_warns = mapping_meaning_warnings(row, reference)
    return bool(row.get('scope') == 'in_scope' and review_status(row) not in {'held', 'rejected'}
                and not row.get('mapping_consistency_warnings')
                and not meaning_warns
                and pt.get('status') not in {'incompatible', 'missing_required_endpoint', 'insufficient_type_info'}
                and not relation.get('under_review')
                and relation.get('mapping') == 'exact' and relation.get('iri')
                and direct_property_allowed(relation.get('iri'))
                and all(row.get('bindings', {}).get(r, {}).get('mapping') in {'exact', 'broader'}
                        and row['bindings'][r].get('iri') for r in ('subject', 'object')))


def refresh_eligibility(row, reference=None):
    row['extraction_review_status'] = review_status(row)
    if reference is not None and hasattr(reference, 'terms'):
        row['property_type_check'] = check_property_types(row, reference)
        row['mapping_review_warnings'] = mapping_meaning_warnings(row, reference)
    for role in ('subject', 'object'):
        binding = row['bindings'][role]
        binding['entity_id'] = ('concept-' + stable_hash(binding['canonical_iri'], 32)
                                if can_share(row, role) else binding['occurrence_id'])
    row['assembly_eligible'] = assertion_eligible(row, reference)
    row['eligibility_policy'] = ELIGIBILITY_POLICY
    return row
