from pathlib import Path
import json
import pytest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"src"))
from tools.context_evaluation import load_cases, freeze_experiment, run_experiment


def test_cases_have_six_pairs_and_neutral_model_inputs():
    cases = load_cases()
    assert len(cases) == 12
    assert {c['group'] for c in cases} == {'development', 'holdout'}
    assert all(len([c for c in cases if c['kind'] == k]) == 2 for k in {c['kind'] for c in cases})
    manifest = freeze_experiment(cases, {'model': 'fake'})
    assert len(manifest['schedule']) == 72
    assert all('expected' not in json.dumps(c['input']) for c in manifest['cases'])


def test_history_not_overwritten(tmp_path):
    output = tmp_path / 'old'
    output.mkdir()
    (output / 'keep').write_text('unchanged')
    with pytest.raises(FileExistsError):
        run_experiment(load_cases(), None, output)
    assert (output / 'keep').read_text() == 'unchanged'
from focusmate.contracts.skill_execution import ProviderResponse
from tools.context_evaluation import generate, prepare_arm


class FakeProvider:
    public_config={'model':'fake','apiKey':'must-not-save'}
    def __init__(self,content=None):
        self.content=content
        self.calls=[]
    def complete(self,**kwargs):
        self.calls.append(kwargs)
        payload=json.loads(kwargs['user_prompt'])
        if 'summarySchema' in payload:
            source=payload['messages'][0]
            result={'items':[{'kind':'userStatement','text':source['text'],'quotes':[{'ref':source['ref'],'text':source['text']}]}]}
            return ProviderResponse(content=json.dumps(result,ensure_ascii=False),model='fake')
        return ProviderResponse(content=self.content or json.dumps({'hint':'请核对当前初始化。','evidenceRefs':['code-current']}),model='fake')


def test_all_organized_cases_really_summarize():
    import time
    provider=FakeProvider()
    for case in load_cases():
        result=prepare_arm(case,'organized',provider,time.monotonic()+120)
        assert result['status']=='ready', result
        assert 'summary' in result
        assert len(result['snapshot']['modelPayload']['dialogue'])==4
    assert len(provider.calls)==12


@pytest.mark.parametrize('content,reason',[
    ('not json','INVALID_JSON'),
    ('{"hint":"x"}','INVALID_OUTPUT_SCHEMA'),
    ('{"hint":"x","evidenceRefs":["m01"]}','INVALID_EVIDENCE_REFERENCES'),
])
def test_bad_response_and_unseen_references_rejected(content,reason):
    entry={'recordId':'r','caseId':'case-01','arm':'recent','repeat':1}
    result=generate(load_cases()[0],entry,FakeProvider(content))
    assert result['status']=='invalid'
    assert result['failureReason']==reason
    assert result['response']['content']==content


def test_provider_failure_does_not_become_skill_failure():
    entry={'recordId':'r','caseId':'case-01','arm':'full','repeat':1}
    result=generate(load_cases()[0],entry,None)
    assert result['status']=='unavailable'
    assert result['generationCalls']==0


def test_full_run_records_and_blind_inputs(tmp_path):
    provider=FakeProvider()
    summary=run_experiment(load_cases(),provider,tmp_path/'run')
    assert summary['counts']=={'generated':72}
    assert summary['generationCalls']==72
    assert summary['summaryCalls']==24
    directory=tmp_path/'run'
    assert len(list((directory/'records').glob('*.json')))==72
    blind=json.loads((directory/'blind-review.json').read_text(encoding='utf-8'))
    assert len(blind['items'])==72
    for item in blind['items']:
        assert not {'group','arm','expected','repeat','caseId'} & item.keys()
        assert set(item['materials'])==set(load_cases()[0]['input'])
    for call in provider.calls:
        assert 'judgment' not in call['user_prompt']
        assert 'independentReview' not in call['user_prompt']
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    assert 'apiKey' not in manifest['modelConfig']


def test_bad_case_blocks_all_calls(tmp_path):
    cases=load_cases();cases[0]['input']['code']['ref']='request'
    provider=FakeProvider()
    with pytest.raises(ValueError):
        run_experiment(cases,provider,tmp_path/'bad')
    assert provider.calls==[]

def test_record_write_failure_stops_new_requests(tmp_path,monkeypatch):
    import tools.context_evaluation as module
    original=module.save
    def failing_save(path,value):
        if Path(path).parent.name=='records':
            raise OSError('disk failure')
        return original(path,value)
    monkeypatch.setattr(module,'save',failing_save)
    provider=FakeProvider()
    def prepare(case,arm,provider,deadline):
        return {'status':'ready','snapshot':{'modelPayload':case['input']}}
    with pytest.raises(OSError):
        run_experiment(load_cases(),provider,tmp_path/'disk',prepare=prepare)
    assert len(provider.calls)<=2
