"""Publish explicitly unvalidated assembly views without touching saved evidence."""
from backend.config.pipeline import PIPELINE_MODE, POST_GROUNDING_STAGES_PAUSED, stage_status
from backend.config.pipeline import ONTOLOGY_GROUNDING_PAUSED
from backend.shared.artifacts import save_json
from backend.stages.graph_construction.graph import write_graphml


def publish_unvalidated_views(output, snapshot):
    if POST_GROUNDING_STAGES_PAUSED:
        # Keep historical graph files intact; do not generate new downstream views.
        return {'nodes': [], 'edges': [], 'status': 'paused',
                'counts': {'nodes': 0, 'edges': 0, 'supported': 0, 'corroborated': 0, 'source_supported': 0}}
    global_graph = snapshot['graph']
    edges = [{**edge, 'id': edge['candidate_id'], 'status': 'unvalidated',
              'external_status': 'not_assessed', 'support_count': 0,
              'contradiction_count': 0, 'claim_text_source_verified': False,
              'supported_pathway_ids': [], 'causal_chain_validated': False}
             for edge in global_graph['edges'] if edge['document_key'] == output.name]
    node_ids = {edge[key] for edge in edges for key in ('source', 'target')}
    graph = {'nodes': [node for node in global_graph['nodes'] if node['id'] in node_ids],
             'edges': edges, 'counts': {'nodes': len(node_ids), 'edges': len(edges),
             'supported': 0, 'corroborated': 0, 'source_supported': 0},
             'validation_stages_paused': True, 'causal_chain_validated': False}
    save_json(output / 'graph.json', graph)
    write_graphml(graph, output / 'knowledge_graph.graphml')
    return graph


def paused_metadata():
    return {'knowledge_building_mode': PIPELINE_MODE,
            'automatic_ontology_grounding_paused': ONTOLOGY_GROUNDING_PAUSED,
            'post_grounding_stages_paused': POST_GROUNDING_STAGES_PAUSED,
            'validation_stages_paused': True, 'stage_status': stage_status(),
            'source_stage': 'paused', 'scope_stage': 'paused', 'external_stage': 'paused',
            'external_validation_target': 'paused', 'externally_evaluated': 0,
            'external_incomplete': 0, 'retrieval_pending': 0}
