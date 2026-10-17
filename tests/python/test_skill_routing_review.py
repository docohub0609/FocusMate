from copy import deepcopy
import pytest
from tools.skill_evaluation import digest
from tools.skill_routing_review import validate


def fixture():
    material = {'userRequest': {'ref': 'u', 'text': 'help'}, 'goalRequirement': {'ref': 'g', 'text': 'local'},
                'problemFacts': [], 'code': {'snapshotRef': 'c', 'text': ''}, 'runEvidence': []}
    answer = {'hint': 'check this', 'evidenceRefs': ['d']}
    item = {'answerId': 'a', 'materials': material, 'dialogue': [{'ref': 'd', 'role': 'user', 'text': 'old claim'}],
            'hintLevel': 1, **answer, 'answerHash': digest(answer)}
    packet = {'experimentHash': 'e', 'items': [item]}
    checks = {key: {'status': 'pass', 'reason': 'local direction', 'quote': 'check', 'evidenceRefs': ['d']}
              for key in ('accuracy', 'focus', 'helpfulness', 'coachingScope')}
    checks['helpfulness']['level'] = 'sufficient'
    review = {'reviewer': 'AI-independent', 'humanReviewed': False, 'experimentHash': 'e',
              'packetHash': digest(packet), 'answers': [{'answerId': 'a', 'answerHash': digest(answer), 'checks': checks}]}
    return packet, review


def test_dialogue_citation_is_validated_without_mutating_packet():
    packet, review = fixture()
    before = deepcopy(packet)
    assert validate(packet, review)['a']
    assert packet == before


@pytest.mark.parametrize('change', ['missing', 'ref', 'quote', 'hash'])
def test_bad_reviews_rejected(change):
    packet, review = fixture()
    check = review['answers'][0]['checks']['accuracy']
    if change == 'missing': del review['answers'][0]['checks']['focus']
    if change == 'ref': check['evidenceRefs'] = ['unknown']
    if change == 'quote': check['quote'] = 'not in answer'
    if change == 'hash': review['packetHash'] = 'other'
    with pytest.raises(ValueError): validate(packet, review)


def create_comparison(tmp_path, phase):
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from tools.skill_routing_cases import get_cases
    from tools.skill_routing_experiment import run
    cases = get_cases(phase)[:2]
    class Fake:
        public_config = {}
        def complete(self, **request):
            body = json.loads(request['user_prompt'])
            value = {'hint': 'check', 'evidenceRefs': ['code:v2']}
            if 'skillDirectory' in body: value['action'] = 'directHint'
            return SimpleNamespace(content=json.dumps(value), model='fake', response_id='fake')
    audit = {'reviewer': 'AI-independent', 'humanReviewed': False, 'cases': [
        {'id': c['id'], 'caseHash': digest(c), 'status': 'accepted', 'reason': 'test fixture only'} for c in cases]}
    directory = tmp_path / phase
    run(cases, Path(__file__).resolve().parents[2] / 'skills/tree-and-graph', 7, Fake(), directory, audit)
    packet = json.loads((directory/'blind-packet.json').read_text(encoding='utf-8'))
    manifest = json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    mapping = {r['answerId']: r['arm'] for r in manifest['schedule']}
    answers = []
    for item in packet['items']:
        checks = {k: {'status': 'pass', 'reason': 'synthetic scoring fixture', 'quote': 'check', 'evidenceRefs': ['code:v2']}
                  for k in ('accuracy','focus','helpfulness','coachingScope')}
        checks['helpfulness']['level'] = 'sufficient'
        if mapping[item['answerId']] == 'ordinary': checks['accuracy']['status'] = 'fail'
        answers.append({'answerId':item['answerId'],'answerHash':item['answerHash'],'checks':checks})
    review = {'reviewer':'AI-independent','humanReviewed':False,'experimentHash':packet['experimentHash'],
              'packetHash':digest(packet),'answers':answers}
    review_path = directory/'review.json'
    review_path.write_text(json.dumps(review),encoding='utf-8')
    return directory, review_path, packet


@pytest.mark.parametrize('phase', ['development', 'holdout'])
def test_gate_needs_holdout_and_complete_developer_review(tmp_path, phase):
    import json
    from tools.skill_routing_review import compare
    directory, review_path, packet = create_comparison(tmp_path, phase)
    assert not compare(directory,[review_path])['meetsExpansionGate']
    developer = {'experimentHash':packet['experimentHash'],'reviewer':'developer-AI','humanReviewed':False,
                 'reviewedAnswerIds':[i['answerId'] for i in packet['items']], 'unresolvedAnswerIds':[]}
    (directory/'developer-review.json').write_text(json.dumps(developer),encoding='utf-8')
    assert compare(directory,[review_path])['meetsExpansionGate'] == (phase == 'holdout')


def test_altered_group_mapping_cannot_reuse_review(tmp_path):
    import json
    from tools.skill_routing_review import compare
    directory, review_path, packet = create_comparison(tmp_path, 'development')
    path = directory/'manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    manifest['schedule'][0]['arm'] = 'selective'
    path.write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError): compare(directory,[review_path])


def test_nulling_answer_cannot_remove_it_from_review_coverage(tmp_path):
    import json
    from tools.skill_routing_review import compare
    directory, review_path, packet = create_comparison(tmp_path, 'development')
    packet['items'][0]['hint'] = None
    (directory/'blind-packet.json').write_text(json.dumps(packet),encoding='utf-8')
    with pytest.raises(ValueError): compare(directory,[review_path])
