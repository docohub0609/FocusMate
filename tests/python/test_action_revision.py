import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools'))
from action_revision_cases import cases
from action_cases import inputs
from action_revision_comparison import configurations


def test_three_arms_change_only_prompts_then_issue_tracking():
    arms=configurations('old actor','old reviewer')
    assert len(arms)==3
    assert arms[0].actor_prompt=='old actor' and not arms[0].track_review_issues
    assert arms[1].actor_prompt==arms[2].actor_prompt
    assert arms[1].review_prompt==arms[2].review_prompt
    assert not arms[1].track_review_issues and arms[2].track_review_issues


def test_fresh_case_expectations_do_not_enter_model_material():
    import json
    for case in cases():
        bundle,diagnosis=inputs(case)
        payload=json.dumps([bundle,diagnosis],ensure_ascii=False)
        assert case['expected'] not in payload
        assert 'expected' not in bundle
    assert len(cases())==12
