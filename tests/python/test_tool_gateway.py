from datetime import datetime, timezone, timedelta
from copy import deepcopy
import hashlib
import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'src'))
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from focusmate.core.tool_gateway import ToolGateway


def context():
    return {'loopId':'l1','goalContext':{'goalId':'g1','scope':'subgoal','subgoalId':'s1'},
        'problemRef':{'problemId':'p1','problemVersion':1},'allowedCodeVersions':[2],
        'allowedTools':['search_code','read_code_excerpt','read_submission_result','run_public_samples','run_local_code'],
        'deadlineAt':(datetime.now(timezone.utc)+timedelta(seconds=90)).isoformat()}

class Backend:
    def __init__(self, source='a=1\nprint(a)\n'):
        self.source=source; self.runs=0
    def load_code(self,ctx,version):
        return {'codeVersion':version,'contentHash':hashlib.sha256(self.source.encode()).hexdigest(),'sourceCode':self.source}
    def load_submission(self,ctx,identity):
        return {'submissionId':identity,'codeVersion':1,'problemRef':ctx['problemRef'],'status':'finished','verdict':'AC'}
    def run_public(self,ctx,code):
        self.runs+=1
        return {'status':'passed','codeVersion':code['codeVersion'],'caseResults':[]}
    def run_local(self,ctx,code,args):
        self.runs+=1
        return {'status':'succeeded','codeVersion':code['codeVersion'],
                'goalContext':ctx['goalContext'],'problemRef':ctx['problemRef'],
                'sourceRef':{'kind':'lines','startLine':1,'endLine':1,'source':'a=1'},
                'setup':args.get('initialVariables',{}),'observations':{'a':1}}

def call(name,args,identity='c1'):
    return {'callId':identity,'toolName':name,'arguments':args}

def test_read_and_search_version_and_lines(tmp_path):
    g=ToolGateway(Backend(),SQLiteToolLedger(tmp_path/'calls.db'));ctx=context()
    r=g.invoke(ctx,call('read_code_excerpt',{'codeVersion':2,'startLine':1,'endLine':2}))
    assert r['status']=='succeeded'
    assert r['data']['lines']==[{'line':1,'text':'a=1'},{'line':2,'text':'print(a)'}]
    s=g.invoke(ctx,call('search_code',{'codeVersion':2,'query':'a'},'c2'))
    assert s['data']['matches']==[1,2]

def test_no_permission_or_wrong_version_never_runs(tmp_path):
    b=Backend();g=ToolGateway(b,SQLiteToolLedger(tmp_path/'c.db'));ctx=context();ctx['allowedTools']=['read_code_excerpt']
    assert g.invoke(ctx,call('run_public_samples',{'codeVersion':2}))['status']=='rejected'
    assert g.invoke(ctx,call('read_code_excerpt',{'codeVersion':1,'startLine':1,'endLine':1},'c2'))['status']=='rejected'
    assert b.runs==0

def test_old_submission_is_valid_evidence_not_current_code(tmp_path):
    r=ToolGateway(Backend(),SQLiteToolLedger(tmp_path/'c.db')).invoke(context(),call('read_submission_result',{'submissionId':'s1'}))
    assert r['status']=='succeeded' and r['data']['codeVersion']==1

def test_huge_line_fails_without_half_line(tmp_path):
    r=ToolGateway(Backend('a'*9000),SQLiteToolLedger(tmp_path/'c.db')).invoke(context(),call('read_code_excerpt',{'codeVersion':2,'startLine':1,'endLine':1}))
    assert r['errorCode']=='LINE_TOO_LONG'

def test_persistent_quota_and_scope(tmp_path):
    b=Backend();ctx=context();p=tmp_path/'c.db'
    for n in range(3):
        assert ToolGateway(b,SQLiteToolLedger(p)).invoke(ctx,call('read_submission_result',{'submissionId':'s1'},str(n)))['status']=='succeeded'
    assert ToolGateway(b,SQLiteToolLedger(p)).invoke(ctx,call('read_submission_result',{'submissionId':'s1'},'4'))['errorCode']=='TOOL_LIMIT'
    ctx['goalContext']['goalId']='other'
    assert ToolGateway(b,SQLiteToolLedger(p)).invoke(ctx,call('read_submission_result',{'submissionId':'s1'},'5'))['errorCode']=='CONTEXT_CHANGED'


def test_search_deduplicates_overlapping_context_and_marks_more(tmp_path):
    g=ToolGateway(Backend('\n'.join('dp='+str(i) for i in range(20))),SQLiteToolLedger(tmp_path/'c.db'))
    r=g.invoke(context(),call('search_code',{'codeVersion':2,'query':'dp'}))
    assert r['status']=='succeeded' and len(r['data']['matches'])==10
    assert len({x['line'] for x in r['data']['lines']})==len(r['data']['lines'])
    assert r['data']['truncated']


def test_two_invalid_requests_stop_even_valid_third(tmp_path):
    g=ToolGateway(Backend(),SQLiteToolLedger(tmp_path/'c.db'));ctx=context()
    for n in range(2):
        assert g.invoke(ctx,call('invalid',{},str(n)))['status']=='rejected'
    assert g.invoke(ctx,call('read_submission_result',{'submissionId':'s1'},'3'))['errorCode']=='INVALID_CALL_LIMIT'


def test_deadline_and_native_call_id_conflict(tmp_path):
    g=ToolGateway(Backend(),SQLiteToolLedger(tmp_path/'c.db'));ctx=context()
    g.invoke(ctx,call('read_submission_result',{'submissionId':'s1'}))
    assert g.invoke(ctx,call('read_submission_result',{'submissionId':'s2'}))['errorCode']=='CALL_ID_CONFLICT'
    ctx=context();ctx['loopId']='expired';ctx['deadlineAt']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    assert g.invoke(ctx,call('read_submission_result',{'submissionId':'s1'}))['errorCode']=='DEADLINE_EXCEEDED'


def test_read_length_never_returns_partial_line(tmp_path):
    b=Backend('\n'.join('x'*1000 for _ in range(10)))
    r=ToolGateway(b,SQLiteToolLedger(tmp_path/'c.db')).invoke(context(),call('read_code_excerpt',{'codeVersion':2,'startLine':1,'endLine':10}))
    assert r['data']['truncated']
    assert all(len(x['text'])==1000 for x in r['data']['lines'])


def test_real_readonly_backend_filters_private_and_checks_result_association(tmp_path):
    import sqlite3,json
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    from tools.tool_demo import provision_fixture
    from focusmate.adapters.tool_storage import SQLiteToolBackend
    ctx=context();db=tmp_path/'business.db'
    provision_fixture(db,{'context':ctx,'fixtures':{'codes':[{'version':2,'source':'print(5)'}],
        'submissions':[{'submissionId':'s1','codeVersion':1,'status':'finished','verdict':'AC'}],'publicSamples':[]}})
    b=SQLiteToolBackend(db);before=db.read_bytes()
    assert b.load_code(ctx,2)['sourceCode']=='print(5)'
    r=b.load_submission(ctx,'s1')
    assert r['verdict']=='AC' and 'PRIVATE' not in json.dumps(r)
    assert db.read_bytes()==before
    with sqlite3.connect(db) as connection:
        connection.execute('UPDATE oj_results SET code_version=2')
    import pytest
    with pytest.raises(ValueError,match='RESULT_ASSOCIATION_INVALID'):b.load_submission(ctx,'s1')


def test_missing_business_database_not_created(tmp_path):
    from focusmate.adapters.tool_storage import SQLiteToolBackend
    db=tmp_path/'absent.db';r=ToolGateway(SQLiteToolBackend(db),SQLiteToolLedger(tmp_path/'c.db')).invoke(context(),call('read_submission_result',{'submissionId':'s1'}))
    assert r['status']=='failed' and not db.exists()


def test_public_slot_is_reserved_before_first_execution_finishes(tmp_path):
    from threading import Event,Thread
    entered=Event();finish=Event()
    class Slow(Backend):
        def run_public(self,ctx,code):
            self.runs+=1;entered.set();finish.wait(5)
            return {'status':'succeeded','codeVersion':code['codeVersion']}
    backend=Slow();ctx=context();ctx['allowedCodeVersions']=[1,2]
    gateway=ToolGateway(backend,SQLiteToolLedger(tmp_path/'c.db'))
    t=Thread(target=lambda:gateway.invoke(ctx,call('run_public_samples',{'codeVersion':1},'c1')))
    t.start()
    try:
        assert entered.wait(2)
        r=gateway.invoke(ctx,call('run_public_samples',{'codeVersion':2},'c2'))
        assert r['errorCode']=='RUN_LIMIT' and backend.runs==1
    finally:finish.set();t.join(5)


def test_infrastructure_failure_with_partial_cases_is_not_success(tmp_path):
    class Incomplete(Backend):
        def run_public(self,ctx,code):
            return {'status':'failed','sampleStatus':'infrastructureError',
                'goalContext':ctx['goalContext'],'problemRef':ctx['problemRef'],
                'codeVersion':code['codeVersion'],'caseResults':[{'outcome':'passed'}]}
    result=ToolGateway(Incomplete(),SQLiteToolLedger(tmp_path/'c.db')).invoke(context(),call('run_public_samples',{'codeVersion':2}))
    assert result['status']=='failed'


def test_empty_success_or_contradictory_outcome_rejected(tmp_path):
    class Invalid(Backend):
        def run_public(self,ctx,code):
            return {'status':'succeeded','sampleStatus':'passed',
                'goalContext':ctx['goalContext'],'problemRef':ctx['problemRef'],
                'codeVersion':code['codeVersion'],'caseResults':[]}
    result=ToolGateway(Invalid(),SQLiteToolLedger(tmp_path/'c.db')).invoke(context(),call('run_public_samples',{'codeVersion':2}))
    assert result['errorCode']=='INVALID_RESULT'


def test_submission_status_and_version_checked_before_return(tmp_path):
    class Invalid(Backend):
        def load_submission(self,ctx,identity):
            return {**super().load_submission(ctx,identity),'status':'internal-debug','codeVersion':None}
    result=ToolGateway(Invalid(),SQLiteToolLedger(tmp_path/'c.db')).invoke(context(),call('read_submission_result',{'submissionId':'s1'}))
    assert result['errorCode']=='INVALID_RESULT' and 'data' not in result


def local_args(**changes):
    value={'codeVersion':2,'purpose':'确认赋值结果','observationIntent':'观察 a',
           'snippet':{'kind':'lines','startLine':1,'endLine':1},
           'initialVariables':{},'observe':{'identifiers':['a'],'returnValue':False}}
    value.update(changes);return value


@pytest.mark.parametrize('change', [
    lambda data: data.pop('observations'),
    lambda data: data.update(observations={'other': 1}),
    lambda data: data.update(returnValue=99),
    lambda data: data.update(setup={'a': 9}),
])
def test_local_success_must_match_requested_observations_and_setup(tmp_path, change):
    class Invalid(Backend):
        def run_local(self, ctx, code, args):
            data = super().run_local(ctx, code, args)
            change(data)
            return data
    result = ToolGateway(Invalid(), SQLiteToolLedger(tmp_path/'calls.db')).invoke(
        context(), call('run_local_code', local_args()))
    assert result['errorCode'] == 'INVALID_RESULT'


def test_local_run_checks_permission_version_and_result_context(tmp_path):
    backend=Backend();ctx=context();ctx['allowedTools']=['run_local_code']
    gateway=ToolGateway(backend,SQLiteToolLedger(tmp_path/'c.db'))
    assert gateway.invoke(ctx,call('run_local_code',local_args()))['status']=='succeeded'
    bad=context();bad['allowedTools']=['run_local_code'];bad['allowedCodeVersions']=[1]
    assert ToolGateway(backend,SQLiteToolLedger(tmp_path/'d.db')).invoke(bad,call('run_local_code',local_args()))['errorCode']=='NOT_FOUND_OR_FORBIDDEN'


def test_local_run_reservation_identity_includes_setup_call_and_observe(tmp_path):
    backend=Backend();ctx=context();ctx['allowedTools']=['run_local_code']
    gateway=ToolGateway(backend,SQLiteToolLedger(tmp_path/'c.db'))
    assert gateway.invoke(ctx,call('run_local_code',local_args(),'one'))['status']=='succeeded'
    changed=local_args(initialVariables={'a':9})
    assert gateway.invoke(ctx,call('run_local_code',changed,'two'))['errorCode']=='RUN_LIMIT'
    assert backend.runs==1
