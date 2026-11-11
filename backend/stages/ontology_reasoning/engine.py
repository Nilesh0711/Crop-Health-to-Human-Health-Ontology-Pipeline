"""A deliberately explicit, provenance-bearing fragment of RDFS/OWL reasoning.

Supported: named subclass/equivalent-class, subproperty/equivalent-property,
instance classification, domain/range typing and inverse properties. Biological
rules are separately supplied, reviewed and guarded by identical nonempty study
context. OWL restrictions/property chains and full OWL consistency are NOT claimed.
"""
from collections import defaultdict
import json

from rdflib import URIRef, BNode
from rdflib.namespace import RDF, RDFS, OWL

from backend.shared.storage import stable_hash, utc_now
from backend.shared.cancellation import check_cancelled

SUBCLASS, SUBPROPERTY, TYPE = map(str, (RDFS.subClassOf, RDFS.subPropertyOf, RDF.type))
FORMAL = {str(RDFS.subClassOf), str(RDFS.subPropertyOf), str(OWL.equivalentClass),
          str(OWL.equivalentProperty), str(OWL.inverseOf), str(RDFS.domain), str(RDFS.range), str(OWL.disjointWith)}


def fact_id(s, p, o, context=''):
    return 'fact-' + stable_hash(json.dumps([s, p, o, context], ensure_ascii=False), 32)


def validate_rule(rule, catalog):
    required = {'id', 'body', 'head', 'reviewed_by', 'review_note', 'evidence_refs', 'same_context'}
    if not required <= rule.keys() or rule['same_context'] is not True:
        raise ValueError('A biological rule needs explicit premises/head, same_context=true and review provenance')
    if not rule['reviewed_by'].strip() or not rule['review_note'].strip() or not rule['evidence_refs']:
        raise ValueError('No biological rule may run without an attributed review and evidence references')
    if not 1 <= len(rule['body']) <= 6 or len(rule['head']) != 3:
        raise ValueError('Rules require 1–6 triple premises and one triple conclusion')
    allowed = set(catalog.profile['allowed_object_properties']) | {TYPE}
    bound = set()
    for triple in rule['body']:
        if len(triple) != 3 or triple[1] not in allowed:
            raise ValueError('Rule predicates must be approved property identifiers, not variables')
        bound.update(v for v in triple if str(v).startswith('?'))
    if rule['head'][1] not in allowed or any(v.startswith('?') and v not in bound for v in rule['head']):
        raise ValueError('Rule head must use an approved property and variables bound by its premises')
    return True


def reason(catalog, assertions, *, max_facts=20000):
    facts, keys = {}, {}
    unsupported = defaultdict(int)
    def add(s, p, o, *, context='', rule=None, premises=(), source=None, scientific='ontology_axiom', conditions=None):
        key = (str(s), str(p), str(o), context)
        if key in keys:
            return False
        if len(facts) >= max_facts:
            raise ValueError('Reasoning fact budget exceeded; narrow the ontology/rules. No partial closure was published.')
        identifier = fact_id(*key)
        row = {'id': identifier, 'subject': str(s), 'predicate': str(p), 'object': str(o),
               'context_signature': context, 'kind': 'derived' if rule else 'asserted',
               'rule': rule, 'premises': list(premises), 'source': source,
               'scientific_status': scientific, 'conditions': conditions or {}}
        facts[identifier], keys[key] = row, identifier
        return True

    for s, p, o in catalog.graph:
        if str(p) in FORMAL:
            if isinstance(s, URIRef) and isinstance(o, URIRef):
                add(s, p, o, source={'catalog_signature': catalog.signature, 'axiom': [str(s), str(p), str(o)],
                                     'reference_sources': catalog.axiom_sources.get((str(s), str(p), str(o)), [])})
            else:
                unsupported[str(p)] += 1
        elif isinstance(s, BNode) and p in {OWL.onProperty, OWL.someValuesFrom, OWL.allValuesFrom,
                                          OWL.intersectionOf, OWL.propertyChainAxiom}:
            unsupported[str(p)] += 1
    for row in assertions:
        if row.get('scope') != 'in_scope':
            continue
        bindings, original = row['bindings'], row['original_claim']
        context = stable_hash(json.dumps({'document': row['document_key'], 'qualifiers': original.get('qualifiers') or {},
            'local_context': original.get('source_local_context') or original.get('source_evidence_span'),
            'source_wording': original.get('source_evidence_span')}, sort_keys=True, ensure_ascii=False), 64)
        for role in ('subject', 'object'):
            binding = bindings[role]
            if binding['mapping'] == 'exact' and binding['denotation'] == 'particular':
                add('urn:soil-crop-health:' + binding['occurrence_id'], TYPE, binding['iri'],
                    source={'candidate_id': row['candidate_id'], 'document_key': row['document_key'], 'role': role},
                    scientific='provisional_mapping_not_source_validated')
        if row.get('assembly_eligible'):
            add('urn:soil-crop-health:' + bindings['subject']['entity_id'], bindings['relation']['iri'],
                'urn:soil-crop-health:' + bindings['object']['entity_id'], context=context,
                source={'candidate_id': row['candidate_id'], 'document_key': row['document_key'],
                        'claim_signature': row['claim_signature']}, conditions=original.get('qualifiers') or {},
                scientific='unvalidated_extracted_assertion')
    # Formal closure is restricted to ontology axioms and occurrence typing.
    # Generic scientific assertions stay reified/context-bound; inverse/subproperty
    # consequences inherit their context and unvalidated status.
    changed = True
    while changed:
        check_cancelled()
        changed = False
        rows = list(facts.values())
        by_pred = defaultdict(list)
        for f in rows:
            by_pred[f['predicate']].append(f)
        for equivalence, relation in ((str(OWL.equivalentClass), SUBCLASS), (str(OWL.equivalentProperty), SUBPROPERTY)):
            for ax in by_pred[equivalence]:
                for s, o in ((ax['subject'], ax['object']), (ax['object'], ax['subject'])):
                    changed |= add(s, relation, o, rule='equivalence_expansion', premises=[ax['id']])
        for relation in (SUBCLASS, SUBPROPERTY):
            outgoing = defaultdict(list)
            for ax in by_pred[relation]:
                outgoing[ax['subject']].append(ax)
            for first in by_pred[relation]:
                for second in outgoing[first['object']]:
                    if first['subject'] != second['object']:
                        changed |= add(first['subject'], relation, second['object'], rule='hierarchy_transitivity',
                                       premises=[first['id'], second['id']])
        for f in rows:
            for ax in by_pred[SUBCLASS] if f['predicate'] == TYPE else []:
                if f['object'] == ax['subject']:
                    changed |= add(f['subject'], TYPE, ax['object'], rule='instance_classification',
                        premises=[f['id'], ax['id']], scientific=f['scientific_status'], source=f['source'])
            for ax in by_pred[SUBPROPERTY]:
                if f['predicate'] == ax['subject']:
                    changed |= add(f['subject'], ax['object'], f['object'], context=f['context_signature'],
                        rule='subproperty_application', premises=[f['id'], ax['id']], scientific=f['scientific_status'], conditions=f['conditions'])
            for ax in by_pred[str(OWL.inverseOf)]:
                other = ax['object'] if f['predicate'] == ax['subject'] else ax['subject'] if f['predicate'] == ax['object'] else None
                if other:
                    changed |= add(f['object'], other, f['subject'], context=f['context_signature'], rule='inverse_property',
                        premises=[f['id'], ax['id']], scientific=f['scientific_status'], conditions=f['conditions'])
            for predicate, target in ((str(RDFS.domain), 'subject'), (str(RDFS.range), 'object')):
                for ax in by_pred[predicate]:
                    # Domain/range are type inferences, not hard input validation.
                    if f['predicate'] == ax['subject'] and not f['context_signature']:
                        changed |= add(f[target], TYPE, ax['object'], rule='property_typing',
                            premises=[f['id'], ax['id']], scientific=f['scientific_status'])

    biological_rules = [r for r in catalog.profile.get('biological_rules', []) if r.get('enabled')]
    for rule in biological_rules:
        validate_rule(rule, catalog)
    # Execute reviewed rules to a guarded fixpoint; never join different contexts.
    changed = bool(biological_rules)
    while changed:
        check_cancelled()
        changed = False
        for rule in biological_rules:
            states = [({}, [], None, {})]
            for pattern in rule['body']:
                matches = []
                for binding, premises, context, conditions in states:
                    for f in list(facts.values()):
                        if not f['context_signature'] or context not in (None, f['context_signature']):
                            continue
                        candidate = dict(binding)
                        for part, value in zip(pattern, (f['subject'], f['predicate'], f['object'])):
                            if part.startswith('?'):
                                if part in candidate and candidate[part] != value:
                                    break
                                candidate[part] = value
                            elif part != value:
                                break
                        else:
                            matches.append((candidate, premises + [f['id']], f['context_signature'], f['conditions']))
                states = matches
            for bindings, premises, context, conditions in states:
                s, p, o = [bindings.get(value, value) for value in rule['head']]
                changed |= add(s, p, o, context=context, rule=rule['id'], premises=premises,
                    source={'reviewed_by': rule['reviewed_by'], 'evidence_refs': rule['evidence_refs']},
                    conditions=conditions, scientific='conditional_inference_from_unvalidated_premises')
    issues = []
    supers = defaultdict(set)
    for f in facts.values():
        if f['predicate'] == SUBCLASS:
            supers[f['subject']].update({f['subject'], f['object']})
    disjoint = [f for f in facts.values() if f['predicate'] == str(OWL.disjointWith)]
    for cls, ancestors in supers.items():
        for ax in disjoint:
            if {ax['subject'], ax['object']} <= ancestors:
                issues.append({'class': cls, 'kind': 'unsatisfiable_class_in_supported_fragment',
                               'disjoint_ancestors': [ax['subject'], ax['object']]})
    types = defaultdict(set)
    for f in facts.values():
        if f['predicate'] == TYPE:
            types[f['subject']].add(f['object'])
    for individual, classes in types.items():
        for left in classes:
            for right in classes:
                if left < right and catalog.incompatible(left, right):
                    issues.append({'individual': individual, 'kind': 'declared_disjoint_types', 'types': [left, right]})
    return {'policy': 'traceable-named-axioms-and-guarded-rules-v1', 'completed_at': utc_now(),
        'catalog_signature': catalog.signature, 'rules_signature': catalog.rules_signature,
        'facts': list(facts.values()), 'issues': issues,
        'coverage': {'supported_fragment': ['named_subclass', 'named_equivalence', 'named_subproperty',
                     'instance_classification', 'inverse_property', 'domain_range_typing'],
                     'unsupported_axiom_counts': dict(unsupported), 'complete_owl_consistency': False,
                     'ontology_scope': catalog.manifest.get('coverage'), 'biological_rules_enabled': len(biological_rules)},
        'counts': {'asserted': sum(f['kind'] == 'asserted' for f in facts.values()),
                   'derived': sum(f['kind'] == 'derived' for f in facts.values()), 'issues': len(issues)},
        'scientific_validation': 'paused', 'whole_pathway_causation': 'not_established'}
