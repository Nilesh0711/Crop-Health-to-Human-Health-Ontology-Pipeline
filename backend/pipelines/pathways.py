"""Run global assembly over saved claims without repeating extraction/validation."""
import json
from dataclasses import replace

from backend.config.settings import Settings
from backend.providers.llm import OpenCodeClient, format_request_metrics, request_log_level
from backend.shared.artifacts import save_rows
from backend.shared.storage import read_jsonl, utc_now
from backend.stages.pathway_assembly.assembly import assemble_pathways


def assemble_saved_pathways(root, output, emit, thinking=None):
    settings = replace(Settings(), thinking=thinking) if thinking is not None else Settings()
    rows = read_jsonl(output / 'llm_requests.jsonl')
    def log(metrics):
        rows.append({'at':utc_now(), **metrics})
        save_rows(output / 'llm_requests.jsonl', rows)
        emit(request_log_level(metrics), format_request_metrics(metrics))
    client = OpenCodeClient(settings, task='pathway_assembly', on_request=log,
        on_progress=lambda event:emit('progress', json.dumps(event)), response_dir=output / 'model_responses')
    snapshot = assemble_pathways(root, client, settings, emit)
    return {'run_status':'completed', 'pathway_assembly':snapshot['counts']}
