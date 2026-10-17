import json
import sqlite3
from test_progress_storage import database
from test_process_verification import inputs
from focusmate.core.context_builder import hash_content


def test_reads_full_frozen_requirements_without_changing_business_db(tmp_path):
    from focusmate.adapters.process_storage import SQLiteProcessResolver
    path,old,oldrecords=database(tmp_path)
    req,records,_=inputs()
    with sqlite3.connect(path) as db:
        db.execute('UPDATE goal_contract_versions SET compilation_json=?',(json.dumps(records[0]['content']),))
    for s in req['sources']:
        s.update(sourceType={'goal':'contract','code':'code','dialogue':'external'}[s['kind']],
                 sourceId='g1' if s['kind']=='goal' else s['ref'])
    before=path.read_bytes()
    with SQLiteProcessResolver(path,req,supplemental_records=[records[-1]]) as resolver:
        got=resolver.resolve('g01')
        assert len(got['content']['rubrics'][0]['checks'])==2
        assert got['goalContext']=={'goalId':'g1','scope':'goal'}
        assert got['contentHash']==hash_content(records[0]['content'])
    assert path.read_bytes()==before
