from copy import deepcopy
import pytest


def mutation(key='new:boundary', **changes):
    return dict(kind='create', key=key, expectedVersion=None, objective='不足k个节点保持原顺序',
                checks=['剩余节点不足k时不反转'], evidenceRefs=['message:1'],
                acceptableEvidence=['codeStructureCheck', 'publicSampleRunResult'],
                dependsOn=[], relatedTo=[], replaces=[], reason='用户询问边界', **changes)


def apply(goals, changes):
    import focusmate.core.runtime_subgoals as module
    return module.apply_changes(goals, changes, goal_id='g', available_refs={'message:1'})


def test_create_reuse_revision_and_historical_evidence():
    goals, aliases = apply({}, [mutation()])
    key = aliases['new:boundary']
    assert goals[key]['goalId'] == 'g'
    assert goals[key]['versions'][0]['version'] == 1
    old = deepcopy(goals)
    change = mutation()
    change.update(kind='revise', key=key, expectedVersion=1, checks=['剩余不足k保持原顺序；恰好k应反转'])
    updated, _ = apply(goals, [change])
    assert goals == old
    assert updated[key]['versions'][0] == old[key]['versions'][0]
    assert updated[key]['versions'][-1]['version'] == 2
    with pytest.raises(ValueError, match='STALE'):
        apply(updated, [change])


def test_dependencies_and_replacement_are_atomic():
    a, b = mutation(), mutation('new:move')
    b['dependsOn'] = ['new:boundary']
    goals, aliases = apply({}, [a, b])
    assert goals[aliases['new:move']]['versions'][-1]['dependsOn'] == [aliases['new:boundary']]
    a['dependsOn'] = ['new:move']
    with pytest.raises(ValueError, match='CYCLE'):
        apply({}, [a, b])
    a['dependsOn'] = ['foreign']
    with pytest.raises(ValueError, match='REFERENCE'):
        apply({}, [a])
    with pytest.raises(ValueError, match='DEPENDENCY'):
        apply(goals, [dict(kind='withdraw', key=aliases['new:boundary'], expectedVersion=1, reason='换路线')])


def test_no_goal_is_valid_and_replacement_is_not_verified():
    assert apply({}, []) == ({}, {})
    goals, ids = apply({}, [mutation()])
    new = mutation('new:other')
    new['replaces'] = [ids['new:boundary']]
    result, _ = apply(goals, [new])
    assert result[ids['new:boundary']]['status'] == 'replaced'
    assert result[ids['new:boundary']]['versions'] == goals[ids['new:boundary']]['versions']
    key=next(k for k,v in result.items() if v['status']=='active')
    reused,_=apply(result,[{'kind':'reuse','key':key,'expectedVersion':1,'reason':'问题未解决'}])
    assert reused==result
