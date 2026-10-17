"""Opt-in real Docker checks of the production Run CLI and restart cleanup."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from services.oj_worker.editor_runs import RunFailure
from tests.python.test_editor_runs import setup_run

pytestmark = pytest.mark.skipif(os.getenv('RUN_DOCKER_OJ_INTEGRATION') != '1', reason='explicit Docker opt-in required')


def run_cli(runs, root):
    env = dict(os.environ, FOCUSMATE_DB_PATH=str(runs.path), FOCUSMATE_OJ_WORKSPACE_ROOT=str(root))
    process = subprocess.run([sys.executable, '-m', 'services.oj_worker', '--task-kind', 'runs', '--once'],
                             env=env, capture_output=True, text=True, timeout=60)
    assert process.returncode == 0, process.stderr


@pytest.mark.parametrize('mode', ['customInput', 'publicExample'])
def test_production_cli_runs_without_private_assets_and_cleans_up(tmp_path, mode):
    runs, request = setup_run(tmp_path, mode)
    if mode == 'publicExample':
        from apps.api.editor_storage import EditorRepository
        saved = EditorRepository(runs.path).save_draft(request['goalContext']['goalId'],
            dict(sourceCode='print("1 2")', expectedRevision=1, operationId='public-source'))
        request['codeVersion'] = saved['codeVersion']
    task = runs.enqueue(request)
    run_cli(runs, tmp_path/'worker')
    result = runs.get(task['runId'])
    assert result['status'] == 'finished'
    case = result['result']['cases'][0]
    assert case['outcome'] == ('executed' if mode == 'customInput' else 'passed')
    assert case['stdout'] == ('你好\n' if mode == 'customInput' else '1 2\n')
    with sqlite3.connect(runs.path) as db:
        name, cleanup = db.execute('SELECT container_name,cleanup_pending FROM editor_runs').fetchone()
        assert cleanup == 0
        assert db.execute('SELECT verifier_asset_status FROM problem_versions').fetchone()[0] != 'ready'
    assert not (tmp_path/'worker'/'editor-runs'/task['runId']).exists()
    assert subprocess.run(['docker','inspect',name],capture_output=True).returncode != 0


def test_restarted_worker_cleans_expired_real_container_without_reexecution(tmp_path):
    runs, request = setup_run(tmp_path)
    task = runs.enqueue(request); claim = runs.claim()
    root = tmp_path/'worker'; workspace = root/'editor-runs'/task['runId']
    workspace.mkdir(parents=True); (workspace/'main.py').write_text('print("must not rerun")')
    name = 'focusmate-editor-' + task['runId'] + '-' + uuid4().hex
    subprocess.run(['docker','run','-d','--name',name,'--network','none','--read-only','--memory','256m',
                    '--pids-limit','32','--user','65534:65534','focusmate-python-runner:v1',
                    'python','-I','-S','-c','import time; time.sleep(60)'],check=True,capture_output=True,timeout=20)
    try:
        with sqlite3.connect(runs.path) as db:
            db.execute('UPDATE editor_runs SET container_name=?,workspace=?,cleanup_pending=1,lease_until=0', (name,str(workspace)))
        run_cli(runs, root)
        assert runs.get(task['runId'])['status'] == 'interrupted'
        assert runs.get(task['runId'])['result'] is None
        assert not workspace.exists()
        assert subprocess.run(['docker','inspect',name],capture_output=True).returncode != 0
        with pytest.raises(RunFailure): runs.finish(claim,'finished',{'cases':[]})
        assert runs.claim() is None
    finally:
        subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=10)
