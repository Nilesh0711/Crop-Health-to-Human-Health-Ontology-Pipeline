"""Keep stage ownership and dependency direction enforceable as the backend grows."""
import ast
import subprocess
import sys

from backend.config.paths import BACKEND_DIR, EXTRACTION_PROMPT, EXTERNAL_VALIDATION_PROMPT


def test_active_backend_has_no_undefined_variables():
    result = subprocess.run([sys.executable, '-m', 'ruff', 'check', 'backend'],
        cwd=BACKEND_DIR.parent, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def backend_imports(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or '').startswith('backend'):
            yield node.module
        elif isinstance(node, ast.Import):
            yield from (a.name for a in node.names if a.name.startswith('backend'))


def test_stage_and_shared_modules_do_not_depend_on_entry_points_or_legacy():
    for folder in ('stages', 'domain', 'providers', 'shared', 'config'):
        for path in (BACKEND_DIR / folder).rglob('*.py'):
            for dependency in backend_imports(path):
                assert not dependency.startswith(('backend.api', 'backend.pipelines', 'backend.cli', 'backend.legacy')), (
                    f'{path.relative_to(BACKEND_DIR)} imports higher-level or inactive code: {dependency}')


def test_backend_module_import_graph_is_acyclic():
    modules = {}
    for path in BACKEND_DIR.rglob('*.py'):
        relative = path.relative_to(BACKEND_DIR.parent).with_suffix('')
        name = '.'.join(relative.parts).removesuffix('.__init__')
        modules[name] = path
    graph = {name: set(backend_imports(path)) & modules.keys() for name, path in modules.items()}
    visited, visiting = set(), []

    def visit(name):
        assert name not in visiting, 'Import cycle: ' + ' -> '.join([*visiting, name])
        if name in visited:
            return
        visiting.append(name)
        for dependency in graph[name]:
            visit(dependency)
        visiting.pop()
        visited.add(name)

    for name in graph:
        visit(name)


def test_backend_root_contains_only_the_stable_asgi_entry_point():
    assert {p.name for p in BACKEND_DIR.glob('*.py')} == {'__init__.py', 'app.py'}
    assert EXTRACTION_PROMPT.parent.name == 'claim_extraction' and EXTRACTION_PROMPT.is_file()
    assert EXTERNAL_VALIDATION_PROMPT.parent.name == 'external_validation' and EXTERNAL_VALIDATION_PROMPT.is_file()
    from backend.app import app as stable_app
    from backend.api.app import app
    assert stable_app is app


def test_retired_model_transports_and_gates_cannot_be_selected():
    from dataclasses import fields
    from backend.config.settings import Settings
    from backend.providers import llm
    assert not (BACKEND_DIR / 'legacy').exists()
    assert not hasattr(llm, 'OllamaClient')
    assert not hasattr(llm.OpenCodeClient, '_chat_json_once')
    assert not hasattr(llm, 'subprocess')
    assert not {'model', 'ollama_url', 'scope_model', 'semantic_model', 'helper_model',
                'validation_model', 'external_priority_model', 'external_priority_articles_per_claim',
                'opencode_transport', 'opencode_command', 'opencode_server_url',
                'opencode_agent', 'knowledge_building_mode', 'external_judgement_mode',
                'trust_batch_size', 'paper_ready_judgement', 'query_batch_size',
                'external_prefetch', 'enable_nli_promotion'} & {f.name for f in fields(Settings)}
    assert set(Settings().task_models) == {'extraction', 'source', 'query', 'external', 'pathway_assembly'}
    for task, model in Settings().task_models.items():
        assert llm.OpenCodeClient(Settings(), task=task).model == model


def test_live_backend_cannot_import_retired_research_executors():
    for path in BACKEND_DIR.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            names = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                     [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            assert not any(name.startswith(('archive.legacy_pipeline','scripts.research.historical')) for name in names), str(path)
    from backend.config.settings import Settings
    assert Settings().knowledge_building_mode == Settings().external_judgement_mode == 'pathway_only'


def test_dependency_manifests_contain_only_installable_requirements():
    from packaging.requirements import Requirement
    for filename in ('requirements.txt', 'requirements-dev.txt', 'deploy/render/requirements.txt'):
        path = BACKEND_DIR.parent / filename
        if not path.exists():
            continue
        for line in path.read_text(encoding='utf-8').splitlines():
            value = line.strip()
            if value and not value.startswith(('#', '-r ')):
                Requirement(value)


def test_local_validation_has_no_global_route_or_nli_execution():
    from backend.config.settings import Settings, APPLICATION_VERSION
    from dataclasses import fields
    names={f.name for f in fields(Settings)}
    assert not {'pathway_max_paths','pathway_max_hops','enable_nli','nli_model','nli_device'} & names
    assert APPLICATION_VERSION=='neighbor-validation-v50'
    assert (BACKEND_DIR / 'stages/neighbor_validation/targets.py').is_file()
    assert not (BACKEND_DIR / 'stages/pathway_validation').exists()
    assert not (BACKEND_DIR / 'stages/trust_graph').exists()
    assert not (BACKEND_DIR / 'providers/nli.py').exists()
