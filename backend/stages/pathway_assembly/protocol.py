"""Request-local IDs and lossless text sharing for model-selected entity groups."""
from collections import Counter, defaultdict
import json

from backend.providers.response_repair import validate_schema
from backend.providers.structured_output import IncompleteResponseError
from backend.stages.pathway_assembly.contract import response_schema

REQUEST_PROTOCOL = 'declared-identity-assignments-v4'


def check_groups(answer, endpoints, registry_ids, assignments=None):
    """Check identity bookkeeping only; never choose a scientific alignment."""
    expected = {row['endpoint_id'] for row in endpoints}
    seen, used = set(), set()
    for index, group in enumerate(answer['groups'], 1):
        incoming, old = group['endpoint_ids'], group['existing_node_ids']
        if (not incoming and not old) or not group['canonical_label'].strip():
            raise IncompleteResponseError(f'Identity group {index} is empty or unnamed')
        repeated_endpoints = sorted({k for k, n in Counter(incoming).items() if n > 1} | seen.intersection(incoming))
        repeated_nodes = sorted({k for k, n in Counter(old).items() if n > 1} | used.intersection(old))
        unknown_endpoints = sorted(set(incoming) - expected)
        unknown_nodes = sorted(set(old) - registry_ids)
        errors = [f'{label}: {", ".join(values[:8])}' for label, values in (
            ('repeated endpoint IDs', repeated_endpoints), ('repeated registry IDs', repeated_nodes),
            ('unknown endpoint IDs', unknown_endpoints), ('unknown registry IDs', unknown_nodes)) if values]
        if errors:
            raise IncompleteResponseError(f'Identity group {index}: ' + '; '.join(errors))
        roles_by_edge = defaultdict(set)
        for eid in incoming:
            if ':' in eid:
                edge_id, role = eid.rsplit(':', 1)
                if role in ('subject', 'object'):
                    roles_by_edge[edge_id].add(role)
        for edge_id, roles in roles_by_edge.items():
            if 'subject' in roles and 'object' in roles:
                raise IncompleteResponseError(
                    f"Identity group {index}: Cannot merge both subject and object of claim '{edge_id}' into one entity"
                )
        seen.update(incoming)
        used.update(old)
        if assignments:
            mismatched = [f'{identifier} must retain {assignments[identifier]}' for identifier in incoming
                          if assignments.get(identifier) not in (None, *old)]
            if mismatched:
                raise IncompleteResponseError('Provisional identity mismatch: ' + '; '.join(mismatched[:8]))
    if seen != expected:
        raise IncompleteResponseError('Missing pathway endpoint assignments: ' + ', '.join(sorted(expected - seen)[:8]))


class IdentityPacket:
    """Translate identifiers and deduplicate exact strings, without changing meaning."""
    def __init__(self, batch, registry, assignments):
        self.batch = batch
        self.registry = registry
        self.endpoint_ids = {f'E{i}': row['endpoint_id'] for i, row in enumerate(batch, 1)}
        self.node_ids = {f'N{i}': identifier for i, identifier in enumerate(registry, 1)}
        nodes_by_id = {identifier: alias for alias, identifier in self.node_ids.items()}
        self.assignments = {alias: nodes_by_id[assignments[identifier]]
                            for alias, identifier in self.endpoint_ids.items() if identifier in assignments}
        texts, refs = {}, {}
        def share_text(row):
            result = dict(row)
            for field in ('evidence', 'local_context', 'claim_text'):
                if field not in result:
                    continue
                value = result.pop(field)
                if value not in refs:
                    reference = f'T{len(refs) + 1}'
                    refs[value] = reference
                    texts[reference] = value
                result[field + '_ref'] = refs[value]
            return result
        claim_map = {}
        for row in batch:
            eid = row.get('edge_id') or (row['endpoint_id'].rsplit(':', 1)[0] if ':' in row['endpoint_id'] else row['endpoint_id'])
            if eid not in claim_map:
                claim_map[eid] = f"C{len(claim_map) + 1}"
        incoming = []
        for alias, row in zip(self.endpoint_ids, batch):
            eid = row.get('edge_id') or (row['endpoint_id'].rsplit(':', 1)[0] if ':' in row['endpoint_id'] else row['endpoint_id'])
            item = {k: v for k, v in row.items() if k != 'edge_id'}
            item.update(endpoint_id=alias, claim_id=claim_map[eid], provisional_node_id=self.assignments.get(alias))
            incoming.append(share_text(item))
        profiles = []
        for alias, identifier in self.node_ids.items():
            profile = registry[identifier]
            profiles.append({**profile, 'node_id': alias,
                'identity_context': [share_text(context) for context in profile['identity_context']]})
        self.payload = {'incoming_endpoints': incoming, 'registry_nodes': profiles, 'source_texts': texts}
        self.schema = response_schema(self.endpoint_ids, self.node_ids)
        self.user = json.dumps(self.payload, ensure_ascii=False)

    def input_characters(self, system):
        return len(system) + len(self.user) + len(json.dumps(self.schema, ensure_ascii=False))

    def groups(self, answer):
        validate_schema(answer, self.schema)
        metadata = {group['group_id']: group for group in answer['groups']}
        if len(metadata) != len(answer['groups']):
            repeated = [str(key) for key, n in Counter(g['group_id'] for g in answer['groups']).items() if n > 1]
            raise IncompleteResponseError('Repeated group metadata IDs: ' + ', '.join(repeated))
        used = set(answer['endpoint_assignments'].values()) | (set(answer['registry_assignments'].values()) - {None})
        missing = used - metadata.keys()
        if missing:
            raise IncompleteResponseError(f'Group metadata must match assigned groups; missing={sorted(missing)}')
        # Prune unused group metadata definitions so harmless extra groups do not abort execution
        metadata = {group_id: group for group_id, group in metadata.items() if group_id in used}
        groups = [{**group,
            'endpoint_ids': [alias for alias, value in answer['endpoint_assignments'].items() if value == group_id],
            'existing_node_ids': [alias for alias, value in answer['registry_assignments'].items() if value == group_id],
            'joins_existing_registry_node': bool([alias for alias, value in answer['registry_assignments'].items() if value == group_id])}
            for group_id, group in metadata.items()]
        check_groups({'groups': groups}, [{'endpoint_id': alias} for alias in self.endpoint_ids],
                     set(self.node_ids), self.assignments)

        # Enforce intra-edge Cannot-Link constraint across both incoming endpoints and joined registry nodes
        for group in groups:
            all_endpoints = set(self.endpoint_ids[alias] for alias in group['endpoint_ids'])
            for node_alias in group['existing_node_ids']:
                raw_node_id = self.node_ids[node_alias]
                all_endpoints.update(self.registry.get(raw_node_id, {}).get('endpoint_ids', []))

            roles_by_edge = defaultdict(set)
            for eid in all_endpoints:
                if ':' in eid:
                    edge_id, role = eid.rsplit(':', 1)
                    if role in ('subject', 'object'):
                        roles_by_edge[edge_id].add(role)

            for edge_id, roles in roles_by_edge.items():
                if 'subject' in roles and 'object' in roles:
                    raise IncompleteResponseError(
                        f"Identity group {group['group_id']} ('{group['canonical_label']}'): Cannot merge both "
                        f"subject and object of the same claim edge '{edge_id}' into one entity node. "
                        f"Subject and object of a single claim must remain distinct entities to prevent invalid self-loops."
                    )

        # Only translate validated selections. Never remove a duplicate, pick a
        # winning identity, or infer an equivalence from names or explanation text.
        return [{k: v for k, v in group.items() if k != 'group_id'} | {
            'endpoint_ids': [self.endpoint_ids[alias] for alias in group['endpoint_ids']],
            'existing_node_ids': [self.node_ids[alias] for alias in group['existing_node_ids']]}
            for group in groups]
