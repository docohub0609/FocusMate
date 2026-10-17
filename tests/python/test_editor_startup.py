import os
import subprocess
import sys


def test_development_bootstrap_allows_both_workers_on_fresh_install(tmp_path):
    env = dict(os.environ, FOCUSMATE_DB_PATH=str(tmp_path/'new-directory'/'app.db'),
               FOCUSMATE_OJ_WORKSPACE_ROOT=str(tmp_path/'work'))
    initialized = subprocess.run([sys.executable, '-m', 'tools.dev_database'], env=env, capture_output=True, text=True, timeout=30)
    assert initialized.returncode == 0, initialized.stderr
    for kind in ['runs', 'submissions']:
        result = subprocess.run([sys.executable, '-m', 'services.oj_worker', '--task-kind', kind, '--once'],
                                env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
