from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]

def module():
    path = ROOT / 'tools/demo-goal-contract.py'
    assert path.is_file(), 'T042 must provide a separate lifecycle demo'
    spec = importlib.util.spec_from_file_location('goal_contract_demo', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

def record(status='draft'):
    return {'goalId':'g1','version':1,'draftRevision':2,'status':status,
        'compilation': {'publicGoals':[{'text':'说明返回位置的要求'}],
                        'strategyCandidates':[{'label':'PRIVATE_STRATEGY'}]},
        'reviews':[{'stale':False,'result':{'status':'needsReview',
            'issues':[{'code':'conditionMismatch','message':'RAW_MODEL_SECRET'}],
            'items':[{'reason':'PRIVATE_RATIONALE'}]}}]}

def test_default_summary_hides_all_untrusted_text():
    demo=module()
    text=demo.render_record(record())
    assert 'draft' in text and 'conditionMismatch' in text
    assert 'PRIVATE' not in text and 'RAW_MODEL' not in text
    assert '说明返回位置' not in text
    assert '说明返回位置' in demo.render_record(record(), show_goals=True)
    assert 'PRIVATE_STRATEGY' in demo.render_record(record(), show_internal=True)

def test_missing_database_is_not_created(tmp_path,capsys):
    demo=module(); db=tmp_path/'absent.db'
    assert demo.main(['--db',str(db),'--goal-id','g1','show'])==1
    assert not db.exists()
    assert 'GOAL_INPUT_UNAVAILABLE' in capsys.readouterr().out

def test_file_import_failure_is_safe(tmp_path,capsys):
    demo=module(); source=tmp_path/'bad.json'; source.write_text('{SECRET INPUT',encoding='utf-8')
    class Repo:
        def import_draft(self,*args): raise AssertionError('must not import invalid JSON')
    assert demo.main(['--db',str(tmp_path/'unused'),'--goal-id','g1','import','--input',str(source)],repository=Repo())==1
    assert 'SECRET INPUT' not in capsys.readouterr().out

def test_review_passes_revision_and_does_not_claim_goal_completed(tmp_path,capsys):
    demo=module(); calls=[]; provider=object()
    class Repo:
        def review_and_freeze(self,goal_id,version,model,expected_draft_revision):
            calls.append((goal_id,version,model,expected_draft_revision)); return record('frozen')
    assert demo.main(['--db',str(tmp_path/'unused'),'--goal-id','g1','review','--version','1','--expected-revision','2'],repository=Repo(),provider=provider)==0
    assert calls==[('g1',1,provider,2)]
    output=capsys.readouterr().out
    assert 'frozen' in output and '学习进度' in output
    assert 'Goal 达成' not in output

def test_export_never_overwrites_existing_file(tmp_path,capsys):
    demo=module(); dest=tmp_path/'existing.json'; dest.write_text('keep',encoding='utf-8')
    class Repo:
        def export_editable(self,*args): return {'objective':'edit me'}
    assert demo.main(['--db',str(tmp_path/'unused'),'--goal-id','g1','export','--version','1','--output',str(dest)],repository=Repo())==1
    assert dest.read_text(encoding='utf-8')=='keep'

def test_pending_review_returns_nonzero(tmp_path):
    demo=module()
    class Repo:
        def review_and_freeze(self,*args,**kwargs): return record()
    assert demo.main(['--db',str(tmp_path/'unused'),'--goal-id','g1','review','--version','1','--expected-revision','2'],repository=Repo(),provider=object())==2


def test_live_runner_refuses_existing_results_before_calling_models(tmp_path):
    path=ROOT/'tools/run-goal-contract-integration.py'
    assert path.is_file(), 'T042 must expose repeatable full lifecycle validation'
    spec=importlib.util.spec_from_file_location('goal_contract_live',path)
    runner=importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
    (tmp_path/'summary.json').write_text('keep',encoding='utf-8')
    with pytest.raises(FileExistsError):
        runner.run_examples(tmp_path, None, None)
    assert (tmp_path/'summary.json').read_text(encoding='utf-8')=='keep'


def live_module():
    spec=importlib.util.spec_from_file_location('goal_contract_live_checked',ROOT/'tools/run-goal-contract-integration.py')
    runner=importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
    return runner

class LifecycleReviewProvider:
    def __init__(self, *, unavailable_on_error=False):
        self.unavailable_on_error=unavailable_on_error
    def complete(self, *, user_prompt, **kwargs):
        from focusmate.contracts.goal_compilation import GoalCompilationResponse
        payload=json.loads(user_prompt)
        bad={node['subgoalId'] for node in payload['contract']['subgoals']
             if node['objective']=='返回的两个位置上的数值必须不同。'}
        if bad and self.unavailable_on_error:
            return GoalCompilationResponse('invalid-json','fake','fake-1')
        items=[]
        for scan in payload['scanItems']:
            negative=scan.get('subgoalId') in bad
            items.append({'itemId':scan['itemId'],'verdict':'contradicted' if negative else 'supported',
                'issueTypes':['conditionMismatch'] if negative else [],'reason':'模拟协议测试；不是语义正确性证明。',
                'sourceRefs':[scan.get('evidenceId',payload['sourceCatalog'][0]['evidenceId'])],
                'templateRefs':[],'blocking':negative})
        return GoalCompilationResponse(json.dumps({'items':items},ensure_ascii=False),'fake','fake-1')

def test_full_lifecycle_demo_closes_temporary_databases(tmp_path):
    from tests.python.test_goal_compilation import Provider
    runner=live_module()
    assert runner.run_examples(tmp_path,Provider(),LifecycleReviewProvider())==0
    summary = json.loads((tmp_path/'summary.json').read_text(encoding='utf-8'))
    assert all(item['status']=='passed' and item['completed'] and item['phase']=='finished' for item in summary)

def test_unavailable_review_is_not_counted_as_semantic_detection(tmp_path,monkeypatch):
    from contextlib import closing
    import sqlite3
    from tests.python.test_goal_compilation import Provider
    runner=live_module()
    def closed_rows(path):
        with closing(sqlite3.connect(path)) as db:
            return db.execute('SELECT * FROM goal_records ORDER BY goal_id').fetchall()
    monkeypatch.setattr(runner,'_goal_rows',closed_rows)
    assert runner.run_examples(tmp_path,Provider(),LifecycleReviewProvider(unavailable_on_error=True))==2
    result=json.loads((tmp_path/'two-sum.json').read_text(encoding='utf-8'))
    assert result['status']=='failed'
    assert result['reason']=='incorrectRevisionReviewUnavailable'


@pytest.mark.parametrize('interrupt_on,completed_step,phase', [
    (1, None, 'initialReview'),
    (2, 'initialReview', 'incorrectRevisionReview'),
    (3, 'incorrectRevision', 'correctedRevisionReview'),
])
def test_lifecycle_interruption_keeps_completed_generation_and_review_evidence(
        tmp_path, interrupt_on, completed_step, phase):
    from tests.python.test_goal_compilation import Provider

    class InterruptedReviewer(LifecycleReviewProvider):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            if self.calls == interrupt_on:
                raise KeyboardInterrupt()
            return super().complete(**kwargs)

    with pytest.raises(KeyboardInterrupt):
        live_module().run_examples(tmp_path, Provider(), InterruptedReviewer())
    saved_path = tmp_path / 'two-sum.json'
    assert saved_path.exists(), 'Already completed paid calls must survive interruption.'
    saved = json.loads(saved_path.read_text(encoding='utf-8'))
    assert saved['completed'] is False
    assert saved['status'] == 'failed'
    assert saved['phase'] == phase
    assert saved['interrupted'] is True
    assert saved['generation']['generation']['providerResponseId'] == 'response-1'
    if completed_step:
        assert saved['steps'][completed_step]['reviews']
    summary = json.loads((tmp_path / 'summary.json').read_text(encoding='utf-8'))
    assert len(summary) == 1
    assert summary[0]['completed'] is False
    assert summary[0]['phase'] == phase
    log = [json.loads(line) for line in (tmp_path / 'two-sum.jsonl').read_text(encoding='utf-8').splitlines()]
    assert any(row['phase'] == 'generationCompleted' for row in log)
    assert log[-1] == saved


def test_lifecycle_refuses_an_existing_checkpoint_log_before_calling_models(tmp_path):
    checkpoint = tmp_path / 'two-sum.jsonl'
    checkpoint.write_text('preserved checkpoint', encoding='utf-8')
    with pytest.raises(FileExistsError):
        live_module().run_examples(tmp_path, None, None)
    assert checkpoint.read_text(encoding='utf-8') == 'preserved checkpoint'
    assert not (tmp_path / 'summary.json').exists()


def test_lifecycle_keeps_corrected_review_if_the_final_read_is_interrupted(tmp_path, monkeypatch):
    from apps.api.goal_contract_storage import GoalContractRepository
    from tests.python.test_goal_compilation import Provider
    original_get = GoalContractRepository.get_version

    def interrupted_read(self, goal_id, version):
        result = original_get(self, goal_id, version)
        if result['status'] == 'superseded':
            raise KeyboardInterrupt()
        return result

    monkeypatch.setattr(GoalContractRepository, 'get_version', interrupted_read)
    with pytest.raises(KeyboardInterrupt):
        live_module().run_examples(tmp_path, Provider(), LifecycleReviewProvider())
    saved = json.loads((tmp_path / 'two-sum.json').read_text(encoding='utf-8'))
    assert saved['completed'] is False
    assert saved['status'] == 'failed'
    assert saved['phase'] == 'correctedRevisionReviewCompleted'
    assert saved['steps']['correctedRevision']['status'] == 'frozen'
    assert saved['steps']['correctedRevision']['reviews'][-1]['result']['status'] == 'passed'
