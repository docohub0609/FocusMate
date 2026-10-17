from copy import deepcopy
from test_goal_contract_storage import seeded,repo,install_review,freeze,imported
from focusmate.core.action_planning import REQUEST_POLICY

def test_policy_upgrade_preserves_old_frozen_contract(repo,seeded,monkeypatch):
    install_review(monkeypatch)
    first=freeze(repo,imported(repo,seeded))
    old=deepcopy(repo.get_current(first['goalId']))
    new=repo.create_revision(first['goalId'],old['version'],'用户明确请求时允许完整答案',coach_policy=REQUEST_POLICY)
    assert new['version']==old['version']+1 and new['status']=='draft'
    assert repo.get_current(first['goalId'])==old
    freeze(repo,new)
    assert repo.get_current(first['goalId'])['compilation']['contract']['coachPolicy']==REQUEST_POLICY
    assert repo.get_version(first['goalId'],old['version'])['compilation']['contract']['coachPolicy']=={'completeSolutionAllowed':False}
