import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]


def invoke(tmp_path,value,name):
    request=tmp_path/(name+'.json');request.write_text(json.dumps(value),encoding='utf-8')
    return subprocess.run([sys.executable,str(ROOT/'tools/manage-session.py'),'--manager-db',str(tmp_path/'manager.db'),
                           '--input',str(request),'--output-dir',str(tmp_path/name)],cwd=ROOT,capture_output=True,text=True)


def test_cli_create_command_query_and_no_overwrite(tmp_path):
    result=invoke(tmp_path,{'operation':'create','sessionId':'s','goalContext':{'goalId':'g','scope':'goal'},'problemRef':{'problemId':'p','problemVersion':1},'contractVersion':1,'codeVersion':1,'allowedTools':[]},'create')
    assert result.returncode==0,result.stderr
    result=invoke(tmp_path,{'operation':'command','schemaVersion':'1.0.0','sessionId':'s','requestId':'q','kind':'ask','payload':{'text':'解释'}},'ask')
    assert result.returncode==0,result.stderr
    result=invoke(tmp_path,{'operation':'query','sessionId':'s'},'query')
    assert result.returncode==0,result.stderr
    report=json.loads((tmp_path/'query/report.json').read_text(encoding='utf-8'))
    assert report['result']['messages'][0]['text']=='解释'
    old=(tmp_path/'query/report.json').read_bytes()
    assert invoke(tmp_path,{'operation':'query','sessionId':'s'},'query').returncode!=0
    assert old==(tmp_path/'query/report.json').read_bytes()


def test_cli_private_input_not_dumped(tmp_path):
    result=invoke(tmp_path,{'operation':'query','sessionId':'s','apiKey':'marker-secret-value'},'private')
    assert result.returncode!=0
    assert 'marker-secret-value' not in (tmp_path/'private/report.json').read_text(encoding='utf-8')
