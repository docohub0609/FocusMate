import json
from pathlib import Path
import pytest
from test_progress_estimation import Provider
from tools.progress_evaluation import load_cases, run_evaluation


def test_cases_are_fixed_and_expectations_never_reach_model(tmp_path):
    cases=load_cases();assert len(cases)==24
    assert len([c for c in cases if c['group']=='development'])==12
    p=Provider();out=tmp_path/'eval'
    run_evaluation([cases[0]],p,out,repeats=2)
    assert len(p.calls)==2
    for call in p.calls:
        assert 'mustInclude' not in call['user_prompt'] and 'reviewBasis' not in call['user_prompt']
        assert cases[0]['expected']['reviewBasis'] not in call['user_prompt']
    assert len(list(out.glob('response-*.json')))==2
    original=(out/'manifest.json').read_bytes()
    with pytest.raises(FileExistsError):run_evaluation([cases[0]],p,out)
    assert len(p.calls)==2 and (out/'manifest.json').read_bytes()==original
