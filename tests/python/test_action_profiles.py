import pytest


def test_baseline_does_not_create_a_second_provider():
    from action_profiles import action_profile
    from focusmate.core.action_execution import ActionExecutor
    executor, reviewer, record = action_profile('baseline')
    assert executor is ActionExecutor and reviewer is None
    assert record['profile']=='baseline'


def test_candidate_is_explicit_and_records_both_roles(monkeypatch):
    from action_profiles import action_profile
    from focusmate.adapters.action_review import DeepSeekActionReviewProvider
    from focusmate.core.action_quality import FocusedActionExecutor
    class Reviewer:
        public_config={'thinkingEnabled':True,'maxTokens':8192}
    monkeypatch.setattr(DeepSeekActionReviewProvider,'from_environment',lambda **kw: Reviewer())
    executor, reviewer, record=action_profile('quality-candidate')
    assert executor is FocusedActionExecutor
    assert record['review']==reviewer.public_config and record['experimental'] is True
    with pytest.raises(ValueError):action_profile('auto-upgrade')
