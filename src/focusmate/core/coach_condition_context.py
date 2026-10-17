"""Keep disputed working conditions visible without treating them as code defects."""
from copy import deepcopy


def condition_concerns(state, *, goals=None):
    """Use the latest assessment of each current goal version, never an old version.

    These are model assessments that the planner may correct. They do not change
    the immutable overall contract or independently complete a goal.
    """
    concerns = []
    for key, goal in (goals if goals is not None else state['workGoals']).items():
        if goal['status'] in ('withdrawn', 'replaced'):
            continue
        version = goal['versions'][-1]['version']
        for ref in reversed(goal['verificationRefs']):
            report = state['checks'].get(ref)
            if not report or report.get('workGoalVersion') != version:
                continue
            requirements = {x['checkId']: x['requirement'] for x in report['requirements']}
            checks = [{'checkId': item['checkId'], 'requirement': requirements[item['checkId']],
                       'conditionStatus': item['conditionStatus'], 'reason': item['reason']}
                      for item in report['checks']
                      if item.get('conditionStatus') in ('invalid', 'uncertain')]
            if checks:
                concerns.append({'subgoalId': key, 'workGoalVersion': version, 'reportRef': ref,
                    'goalContext': deepcopy(report['verifierResult']['goalContext']),
                    'codeVersion': report['verifierResult']['inputVersionRefs'].get('codeVersion'),
                    'checks': checks})
            break
    return concerns


def invalid_condition_goal_ids(state, *, goals=None):
    return {item['subgoalId'] for item in condition_concerns(state, goals=goals)
            if any(check['conditionStatus'] == 'invalid' for check in item['checks'])}


def suppress_invalid_followup(action, state, goals):
    """Stop automated requests to fix code against a rejected, unchanged condition.

    Keep the help's context and all history. Explicit rechecking is still allowed;
    a revised goal or a later valid assessment may restore follow-up normally.
    """
    follow = action['followUp']
    if follow['kind'] != 'awaitEvidence':
        return
    blocked = invalid_condition_goal_ids(state, goals=goals)
    remaining = [key for key in follow['criterionIds'] if key not in blocked]
    if remaining == follow['criterionIds']:
        return
    if remaining:
        follow.update(criterionIds=remaining,
            reason='原条件被指出不适用的目标暂停跟进；其余目标仍可补充证据。',
            expectedUserAction='补充其余目标所需的材料后，可以检查。')
    else:
        action['followUp'] = {'kind': 'none', 'reason': '原条件需要重新核对，暂不要求按它修改代码。',
            'criterionIds': [], 'checks': [], 'expectedUserAction': ''}
