from pathlib import Path
import sys
import json
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools'))
from action_cases import cases,inputs
from action_reviewer_cases import fixtures
from focusmate.core.action_planning import CANDIDATE_SCHEMA,REVIEW_SCHEMA
from focusmate.contracts.schema_validation import validate


def test_development_and_holdout_have_twelve_distinct_requests():
    groups=[cases(g) for g in ('development','holdout')]
    assert [len(g) for g in groups]==[12,12]
    assert len({c['question'] for g in groups for c in g})==24
    for g in groups:
        for c in g:
            b,d=inputs(c)
            validate('DiagnosisReport',d)
            text=json.dumps({'bundle':b,'diagnosis':d},ensure_ascii=False)
            assert c['expected'] not in text
            assert 'wrongDiagnosis' not in text and '"expected"' not in text


def test_reviewer_pairs_have_controlled_facts_and_separate_answer_keys():
    values=fixtures();assert len(values)==12
    for i in range(0,12,2):
        bad,good=values[i:i+2]
        assert bad['bundle']['records']==good['bundle']['records']
        assert bad['expected']=='failed' and good['expected']=='passed'
        assert bad['candidate']['content']!=good['candidate']['content']
