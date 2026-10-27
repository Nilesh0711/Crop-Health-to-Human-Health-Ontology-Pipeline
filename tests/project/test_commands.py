import json

import pytest

from backend.cli.evaluation import main
from backend.stages.claim_extraction.prompt import build_extraction_prompt
from backend.config.paths import EXTRACTION_PROMPT, PROJECT_ROOT, PDF_DIR, ONTOLOGY_DIR, CLI_OUTPUT_DIR
from backend.config import settings as config
from backend.shared.storage import read_jsonl, write_jsonl
from backend.config.paths import REPORTS_DIR


def test_prompt_and_existing_public_paths_work_outside_repository_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    prompt = build_extraction_prompt(4)
    assert EXTRACTION_PROMPT.read_text(encoding='utf-8').strip() in prompt
    assert 'Return at most 4' in prompt
    assert config.PROJECT_ROOT == PROJECT_ROOT
    assert config.PDF_DIR == PDF_DIR
    assert config.ONTOLOGY_DIR == ONTOLOGY_DIR
    assert config.CLI_OUTPUT_DIR == CLI_OUTPUT_DIR
    assert CLI_OUTPUT_DIR == REPORTS_DIR / 'external_validation'
    assert EXTRACTION_PROMPT.parent == PROJECT_ROOT / 'backend' / 'stages' / 'claim_extraction'


def test_standalone_cli_builds_and_reads_candidates_in_report_directory(tmp_path, monkeypatch):
    from argparse import Namespace
    from backend.cli import external_validation as cli
    output = tmp_path / 'reports' / 'external_validation'
    candidate = {'candidate_id': 'test', 'subject': 'rice', 'relation': 'has_contaminant', 'object': 'cadmium'}
    monkeypatch.setattr(cli, 'CLI_OUTPUT_DIR', output)
    monkeypatch.setattr(cli, 'build_candidates', lambda path: ([candidate], [], []))
    assert cli.command_build_candidates(Namespace(tuple_file='raw_triplets.txt')) == 0
    assert cli._selected_candidates(Namespace(candidate_id=None, limit=None)) == [candidate]
    assert {p.name for p in output.iterdir()} == {'candidates.jsonl', 'documents.jsonl', 'candidate_parse_errors.jsonl'}
    assert not (tmp_path / 'rag').exists()


def test_offline_evaluation_commands_sample_and_score_gold_without_overwriting(tmp_path, capsys):
    candidate = {'candidate_id': 'one', 'subject': 'rice', 'object': 'cadmium', 'relation': 'has_contaminant',
                 'subject_type': 'Crop', 'object_type': 'Contaminant', 'schema_error': None,
                 'semantic_validation': {'decision': 'not_run'}, 'source_validation': {'decision': 'entailed'}}
    run = tmp_path / 'run'
    write_jsonl(run / 'candidates.jsonl', [candidate])
    output = tmp_path / 'sample.jsonl'
    assert main(['sample', str(run), '--output', str(output)]) == 0
    capsys.readouterr()
    records = read_jsonl(output)
    assert records[0]['gold']['valid_triple'] is None
    records[0]['gold']['valid_triple'] = True
    write_jsonl(output, records)
    annotated = output.read_bytes()
    with pytest.raises(SystemExit) as error:
        main(['sample', str(run), '--output', str(output)])
    assert error.value.code == 2 and output.read_bytes() == annotated
    capsys.readouterr()
    assert main(['score', str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['triple_validity_support'] == 1
    assert report['triple_validity_precision'] == 1.0
