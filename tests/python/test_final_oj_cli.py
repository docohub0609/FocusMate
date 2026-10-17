import json
from pathlib import Path
import subprocess
import sys
import os

from tests.python.test_final_oj import seeded, request, finish, service

ROOT = Path(__file__).resolve().parents[2]


def call(*args):
    return subprocess.run([sys.executable,str(ROOT/'tools/verify-final-oj.py'),*map(str,args)],
                          cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=30,
                          env={**os.environ,'PYTHONIOENCODING':'utf-8'})


def test_cli_wait_query_ac_and_no_overwrite(tmp_path):
    path,_,sub,_ = seeded(tmp_path)
    reqfile = tmp_path/'request.json'; reqfile.write_text(json.dumps(request(sub)),encoding='utf-8')
    out = tmp_path/'first'
    args = ['--db',path,'--request',reqfile,'--output-dir',out]
    p = call(*args)
    assert p.returncode == 0, p.stderr
    assert json.loads((out/'report.json').read_text())['status'] == 'waitingResult'
    saved = (out/'report.json').read_bytes()
    assert call(*args).returncode != 0
    assert (out/'report.json').read_bytes() == saved
    finish(path,tmp_path)
    p = call('--db',path,'--request',reqfile,'--output-dir',tmp_path/'second')
    assert p.returncode == 0,p.stderr
    assert service(path).goal_state('g1')['status'] == 'completed'
    p = call('--db',path,'--goal-id','g1','--operation','state','--output-dir',tmp_path/'state')
    assert p.returncode == 0,p.stderr


def test_cli_missing_database_does_not_create_one(tmp_path):
    path = tmp_path/'missing.db'
    p = call('--db',path,'--operation','state','--goal-id','g1','--output-dir',tmp_path/'out')
    assert p.returncode != 0
    assert not path.exists()
