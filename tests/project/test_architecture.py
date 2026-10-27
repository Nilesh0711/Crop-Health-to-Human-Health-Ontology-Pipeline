"""Keep stage ownership and dependency direction enforceable as the backend grows."""
import ast

from backend.config.paths import BACKEND_DIR, EXTRACTION_PROMPT, EXTERNAL_VALIDATION_PROMPT


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
                'opencode_agent'} & {f.name for f in fields(Settings)}
    assert set(Settings().task_models) == {'extraction', 'source', 'query', 'external', 'pathway_assembly'}
    for task, model in Settings().task_models.items():
        assert llm.OpenCodeClient(Settings(), task=task).model == model
