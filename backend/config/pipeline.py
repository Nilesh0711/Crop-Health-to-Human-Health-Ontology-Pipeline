"""Active research scope while scientific validation is temporarily archived.

This is a deployment policy, not a per-run switch. Historical assessments remain
readable; no skipped stage may be recorded as a successful scientific check.
"""

PIPELINE_MODE = 'ontology_grounded'
ONTOLOGY_GROUNDING_PAUSED = False
MANUAL_GROUNDING_ENABLED = True
GROUNDING_PAUSED_MESSAGE = ('Automatic ontology grounding is paused while atomic extraction is reviewed. '
    'The pipeline stops after extraction. Use Run grounded claims to assess saved extraction explicitly.')
VALIDATION_STAGES_PAUSED = True
POST_GROUNDING_STAGES_PAUSED = True
POST_GROUNDING_PAUSED_MESSAGE = ('The pipeline stops after ontology grounding. Pathway assembly, '
    'graph construction and reasoning are paused while entity mappings are reviewed.')
UNVALIDATED_ASSEMBLY_DIRECTORY = '.grounding_pathways'
ONTOLOGY_WORKSPACE_DIRECTORY = '.ontology_grounding'
UNVALIDATED_ELIGIBILITY_POLICY = 'structural-only-unvalidated-assembly-v1'
PAUSED_MESSAGE = ('Source fidelity and external scientific validation are temporarily archived. '
                  'The former broad project-scope stage is paused; extraction is guided by the potato-nutrition profile. '
                  'Extracted claims and assembled connections '
                  'are scientifically unvalidated. Grounding and logical inference do not establish biological truth. '
                  + (GROUNDING_PAUSED_MESSAGE if ONTOLOGY_GROUNDING_PAUSED else POST_GROUNDING_PAUSED_MESSAGE))


def active_task_models(settings):
    return {task: model for task, model in settings.task_models.items()
            if task in ({'extraction'} if ONTOLOGY_GROUNDING_PAUSED else {'extraction', 'pathway_assembly'})}


def stage_status():
    return {'source_fidelity': 'paused', 'project_scope': 'paused',
            'external_validation': 'paused', 'ontology_grounding': ('manual' if MANUAL_GROUNDING_ENABLED else 'paused') if ONTOLOGY_GROUNDING_PAUSED else 'active',
            'pathway_assembly': 'paused' if POST_GROUNDING_STAGES_PAUSED else 'active',
            'graph_construction': 'paused' if POST_GROUNDING_STAGES_PAUSED else 'active',
            'reasoning': 'paused' if POST_GROUNDING_STAGES_PAUSED else 'active',
            'application_scope': 'assessed_on_manual_grounding' if ONTOLOGY_GROUNDING_PAUSED and MANUAL_GROUNDING_ENABLED
                else 'not_assessed_grounding_paused' if ONTOLOGY_GROUNDING_PAUSED else 'assessed_during_grounding'}
