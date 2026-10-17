from copy import deepcopy

import pytest

from tools.skill_evaluation import digest


def data():
    answer = {'answerId': 'anonymous-1', 'materials': {
        'userRequest': {'ref': 'request', 'text': '改名'},
        'goalRequirement': {'ref': 'goal', 'text': '只改名'},
        'problemFacts': [], 'code': {'snapshotRef': 'code:v2', 'text': 'mid=1'},
        'runEvidence': []}, 'hint': '把 mid 改成 middle。', 'evidenceRefs': ['code:v2']}
    answer['answerHash'] = digest({'hint': answer['hint'], 'evidenceRefs': answer['evidenceRefs']})
    packet = {'experimentHash': 'frozen', 'items': [answer]}
    checks = {key: {'status': 'pass', 'reason': '只修改当前局部变量。',
                   'quote': '把 mid 改成 middle。', 'evidenceRefs': ['code:v2']}
              for key in ['accuracy', 'focus', 'helpfulness', 'coachingScope']}
    checks['helpfulness']['level'] = 'sufficient'
    review = {'reviewer': 'AI-independent', 'humanReviewed': False, 'experimentHash': 'frozen',
              'packetHash': digest(packet),
              'answers': [{'answerId': 'anonymous-1', 'answerHash': answer['answerHash'], 'checks': checks}]}
    return packet, review


def test_review_binds_exact_visible_answer_and_materials():
    from tools.skill_hint_review import validate_review
    packet, review = data()
    assert validate_review(packet, review)['anonymous-1'] == review['answers'][0]
    changed = deepcopy(packet)
    changed['items'][0]['hint'] = '完全不同的回答'
    with pytest.raises(ValueError):
        validate_review(changed, review)
    changed = deepcopy(packet)
    changed['items'][0]['materials']['userRequest']['text'] = '另一个问题'
    with pytest.raises(ValueError):
        validate_review(changed, review)


@pytest.mark.parametrize('change', ['missing', 'extra', 'quote', 'ref', 'hash', 'human', 'duplicate'])
def test_review_rejects_missing_or_untraceable_judgments(change):
    from tools.skill_hint_review import validate_review
    packet, review = data()
    row = review['answers'][0]
    if change == 'missing': del row['checks']['accuracy']
    if change == 'extra': row['checks']['applicability'] = deepcopy(row['checks']['focus'])
    if change == 'quote': row['checks']['accuracy']['quote'] = '原文没说这句'
    if change == 'ref': row['checks']['accuracy']['evidenceRefs'] = ['invented']
    if change == 'hash': row['answerHash'] = 'changed'
    if change == 'human': review['humanReviewed'] = True
    if change == 'duplicate': review['answers'].append(deepcopy(row))
    with pytest.raises(ValueError): validate_review(packet, review)


def test_uncertainty_and_quality_tradeoff_never_become_a_win():
    from tools.skill_hint_review import compare_checks
    _, review = data()
    good = review['answers'][0]['checks']
    assert compare_checks(good, good) == 'tie'
    bad = deepcopy(good)
    bad['accuracy']['status'] = 'fail'
    assert compare_checks(bad, good) == 'improvement'
    assert compare_checks(good, bad) == 'regression'
    uncertain = deepcopy(bad)
    uncertain['focus']['status'] = 'uncertain'
    assert compare_checks(uncertain, good) == 'insufficientEvidence'
    other_bad = deepcopy(good)
    other_bad['helpfulness'].update(status='fail', level='none')
    assert compare_checks(bad, other_bad) == 'insufficientEvidence'


def test_partial_help_is_not_rewarded_for_length_and_missing_output_is_incomplete():
    from tools.skill_hint_review import compare_checks, validate_review
    packet, review = data()
    good = review['answers'][0]['checks']
    partial = deepcopy(good)
    partial['helpfulness'].update(status='fail', level='partial')
    assert compare_checks(partial, good) == 'improvement'
    packet['items'][0]['hint'] = None
    packet['items'][0]['evidenceRefs'] = []
    packet['items'][0]['answerHash'] = digest({'hint': None, 'evidenceRefs': []})
    review['answers'] = []
    review['packetHash'] = digest(packet)
    assert validate_review(packet, review) == {}
    assert compare_checks(None, good) == 'insufficientEvidence'
