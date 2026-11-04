"""Exercise launcher commands without starting a server or calling a provider."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which('bash') or ('C:/Program Files/Git/bin/bash.exe'
    if Path('C:/Program Files/Git/bin/bash.exe').is_file() else None)
pytestmark = pytest.mark.skipif(not BASH, reason='Bash is required for launcher checks')


@pytest.mark.parametrize('reload_value', [None, '1', '0', 'invalid'])
def test_backend_reload_defaults_and_override(tmp_path, reload_value):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    launcher = scripts / 'run_backend.sh'
    shutil.copyfile(ROOT / 'scripts/run_backend.sh', launcher)
    python = tmp_path / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.write_text('#!/usr/bin/env bash\n'
        'if [[ "$1" == "-m" && "$2" == "uvicorn" ]]; then\n'
        '  printf "LAUNCH_ARG=%s\\n" "$@"\n'
        'fi\n', encoding='utf-8', newline='\n')
    python.chmod(0o755)
    environment = {k:v for k,v in os.environ.items() if k != 'BACKEND_RELOAD'}
    if reload_value is not None:
        environment['BACKEND_RELOAD'] = reload_value
    result = subprocess.run([BASH, launcher.as_posix()], env=environment,
        capture_output=True, text=True, timeout=30)
    if reload_value == 'invalid':
        assert result.returncode != 0 and 'BACKEND_RELOAD must be' in result.stderr
        assert 'LAUNCH_ARG=' not in result.stdout
        return
    assert result.returncode == 0, result.stderr
    arguments = [line.removeprefix('LAUNCH_ARG=') for line in result.stdout.splitlines()
                 if line.startswith('LAUNCH_ARG=')]
    assert arguments[:3] == ['-m', 'uvicorn', 'backend.app:app']
    if reload_value == '0':
        assert '--reload' not in arguments
    else:
        assert '--reload' in arguments
        assert Path(arguments[arguments.index('--reload-dir') + 1]).name == 'backend'
        assert arguments.count('--reload-dir') == 1
        assert '*.py' in arguments and '*.md' in arguments


def test_active_shell_scripts_parse():
    for script in (ROOT / 'scripts').glob('*.sh'):
        result = subprocess.run([BASH, '-n', script.as_posix()], capture_output=True,
                                text=True, timeout=10)
        assert result.returncode == 0, f'{script.name}: {result.stderr}'
