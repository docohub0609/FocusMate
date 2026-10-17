import importlib.util
import json
from pathlib import Path
from test_progress_storage import database
from test_progress_estimation import Provider


def cli():
    path=Path(__file__).resolve().parents[2]/'tools/estimate-goal-progress.py'
    spec=importlib.util.spec_from_file_location('progress_cli',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_cli_read_only_and_no_overwrite(tmp_path):
    db,req,records=database(tmp_path);source=tmp_path/'request.json';out=tmp_path/'out'
    source.write_text(json.dumps({'request':req,'supplementalRecords':[records[-1]]}),encoding='utf-8')
    before=db.read_bytes();calls=[]
    def provider():calls.append(1);return Provider()
    args=['--db',str(db),'--request',str(source),'--output-dir',str(out)]
    assert cli().main(args,provider_factory=provider)==0
    saved=(out/'report.json').read_bytes()
    assert cli().main(args,provider_factory=provider)==1
    assert len(calls)==1 and (out/'report.json').read_bytes()==saved and db.read_bytes()==before


def test_cli_missing_database_never_initializes_provider(tmp_path):
    _,req,records=database(tmp_path);source=tmp_path/'request.json'
    source.write_text(json.dumps({'request':req,'supplementalRecords':[records[-1]]}),encoding='utf-8')
    def forbidden():raise AssertionError('provider should not be loaded')
    assert cli().main(['--db',str(tmp_path/'missing.db'),'--request',str(source),'--output-dir',str(tmp_path/'out')],provider_factory=forbidden)==1
    assert not (tmp_path/'missing.db').exists()


def test_cli_rejects_sensitive_top_level_record_metadata(tmp_path):
    db,req,records=database(tmp_path);source=tmp_path/'request.json';out=tmp_path/'out'
    records[-1]['credentials']={'apiKey':'SYNTHETIC_PRIVATE_MARKER'}
    source.write_text(json.dumps({'request':req,'supplementalRecords':[records[-1]]}),encoding='utf-8')
    calls=[]
    def provider():calls.append(1);return Provider()
    result=cli().main(['--db',str(db),'--request',str(source),'--output-dir',str(out)],provider_factory=provider)
    assert result==1 and not calls
    assert 'SYNTHETIC_PRIVATE_MARKER' not in (out/'report.json').read_text(encoding='utf-8')


def test_cli_non_object_supplement_is_an_input_failure(tmp_path):
    db,req,_=database(tmp_path);source=tmp_path/'request.json';out=tmp_path/'out'
    source.write_text(json.dumps({'request':req,'supplementalRecords':[None]}),encoding='utf-8')
    calls=[]
    def provider():calls.append(1);return Provider()
    assert cli().main(['--db',str(db),'--request',str(source),'--output-dir',str(out)],provider_factory=provider)==1
    assert not calls and json.loads((out/'report.json').read_text(encoding='utf-8'))['failure']['stage']=='input'
