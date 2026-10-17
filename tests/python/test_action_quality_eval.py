import json


def test_quality_eval_fixed_inputs_exclude_expectations():
    from action_quality_cases import reviewer_cases, review_payload
    cases=reviewer_cases()
    assert len(cases)==12
    for c in cases:
        payload=review_payload(c)
        assert 'expected' not in payload and 'basis' not in payload
        assert c['id'] not in json.dumps(payload,ensure_ascii=False)
        assert payload['candidate']['content'] in (c['candidate']['content'],)


def test_no_historical_failure_records_are_overwritten(tmp_path):
    from evaluate_action_quality import save
    import pytest
    p=tmp_path/'result.json';save(p,{'first':True})
    with pytest.raises(FileExistsError):save(p,{'second':True})
    assert json.loads(p.read_text())=={'first':True}


def test_fresh_inputs_never_contain_expectations():
    from action_quality_fresh import cases
    from action_cases import inputs
    fixtures=cases()
    assert len(fixtures)==12 and len({c['id'] for c in fixtures})==12
    for case in fixtures:
        bundle,diagnosis=inputs(case)
        payload=json.dumps({'bundle':bundle,'diagnosis':diagnosis},ensure_ascii=False)
        assert case['expected'] not in payload
        assert case['id'] not in payload


def test_flow_arms_share_frozen_material_without_mutation(tmp_path,monkeypatch):
    from copy import deepcopy
    import evaluate_action_quality as evaluation
    from action_quality_fresh import cases
    from action_cases import inputs
    case=cases()[0];bundle,diagnosis=inputs(case);original=deepcopy((bundle,diagnosis))
    calls=[]
    class Provider:pass
    monkeypatch.setattr(evaluation.DeepSeekToolChatProvider,'from_environment',lambda:Provider())
    monkeypatch.setattr(evaluation.DeepSeekActionReviewProvider,'from_environment',lambda **kw:Provider())
    def run(value,provider,path,**kwargs):
        calls.append(deepcopy((value,kwargs['diagnosis'])))
        value.clear();kwargs['diagnosis'].clear()
        return {'currentRoundId':'r','rounds':{'r':{'status':'finished','failure':None,'counts':{'model':2}}},'outbox':[]}
    monkeypatch.setattr(evaluation,'run_diagnosis',run)
    for arm in ('baseline','candidate'):evaluation.run_flow((case,bundle,diagnosis),1,arm,tmp_path)
    assert calls[0]==calls[1]==original and (bundle,diagnosis)==original
