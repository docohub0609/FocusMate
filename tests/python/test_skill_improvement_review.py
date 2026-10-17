from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from tools.skill_evaluation import digest
from tools.skill_improvement_experiment import run, capture_packages
from tools.skill_improvement_review import read_reviews, summarize


def dump(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def fixture(tmp_path, per_skill=1, fail_first=False, selected_skill=None):
    from tools.skill_routing_cases import get_cases
    original = get_cases('development')[0]
    original['goalContext'] = {'goalId':'current','scope':'subgoal','subgoalId':'current'}
    current = capture_packages(Path('skills'))
    packages = {sid: current[sid] for sid in ('tree-and-graph', 'array-and-hash')}
    cases = []
    for sid in packages:
        for n in range(per_skill):
            c = deepcopy(original)
            c.update(id=f'{sid}-{n}', skillId=sid, phase='holdout')
            cases.append(c)
    audit = {'reviewer':'AI-independent','humanReviewed':False,'cases':[
        {'id':c['id'],'caseHash':digest(c),'status':'accepted','reason':'fixture only'} for c in cases]}
    class Fake:
        public_config = {}
        count = 0
        def complete(self, **request):
            self.count += 1
            if fail_first and self.count == 1:
                from apps.api.deepseek_provider import ProviderFailure
                raise ProviderFailure('LLM_UNAVAILABLE', 'offline', False)
            data = json.loads(request['user_prompt'])
            answer = {'hint':'Check this.', 'evidenceRefs':['code:v2']}
            if 'skillDirectory' in data:
                if selected_skill:
                    entry=next(d for d in data['skillDirectory'] if d['skillId']==selected_skill)
                    answer={'action':'useSkill','skillId':entry['skillId'],'version':entry['version'],
                            'sectionIds':[entry['sections'][0]['id']], 'reason':'fixture','evidenceRefs':['code:v2']}
                else: answer['action'] = 'directHint'
            return SimpleNamespace(content=json.dumps(answer),model='fake',response_id='fake')
    directory = tmp_path/'run'
    run(cases,packages,Fake(),directory,audit,workers=1)
    packet=json.loads((directory/'blind-packet.json').read_text(encoding='utf-8'))
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    arms={e['answerId']:e['arm'] for e in manifest['schedule']}
    answers=[]
    for item in packet['items']:
        if item['hint'] is None: continue
        checks={k:{'status':'pass','reason':'synthetic score','quote':'Check','evidenceRefs':['code:v2']}
                for k in ('accuracy','focus','helpfulness','coachingScope')}
        checks['helpfulness']['level']='sufficient'
        if arms[item['answerId']]=='ordinary': checks['accuracy']['status']='fail'
        answers.append({'answerId':item['answerId'],'answerHash':item['answerHash'],'checks':checks})
    review={'reviewer':'AI-independent','humanReviewed':False,'experimentHash':packet['experimentHash'],
            'packetHash':digest(packet),'answers':answers}
    path=directory/'review.json';dump(path,review)
    dump(directory/'developer-review.json',{'experimentHash':packet['experimentHash'],'reviewer':'developer-AI',
         'humanReviewed':False,'reviewedAnswerIds':[i['answerId'] for i in packet['items'] if i['hint'] is not None], 'unresolvedAnswerIds':[]})
    return directory,path,packet


def test_two_different_skills_cannot_pool_one_improvement_each(tmp_path):
    directory,path,_=fixture(tmp_path)
    result=summarize(directory,[path])
    assert len(result['stableSelectiveImprovementCases'])==2
    assert not result['expansionGateMet']
    assert all(not s['expansionGateMet'] for s in result['perSkill'].values())


def test_gate_is_per_skill_and_reading_requires_real_loading(tmp_path):
    directory,path,_=fixture(tmp_path,2)
    result=summarize(directory,[path])
    assert result['expansionGateMet']
    assert not result['readingBenefitGateMet']
    (directory/'developer-review.json').unlink()
    assert not summarize(directory,[path])['expansionGateMet']


@pytest.mark.parametrize('change',['raw','request','material','missing','duplicate'])
def test_cannot_reuse_review_after_tampering_or_omission(tmp_path,change):
    directory,path,packet=fixture(tmp_path)
    if change in ('raw','request'):
        record_path=directory/'record-001.json'
        row=json.loads(record_path.read_text(encoding='utf-8'))
        if change=='raw': row['calls'][-1]['response']['content']='{"hint":"changed"}'
        else: row['calls'][0]['request']['user_prompt']='changed'
        dump(record_path,row)
    elif change=='material':
        packet['items'][0]['materials']['userRequest']['text']='changed'
        dump(directory/'blind-packet.json',packet)
    elif change=='missing':
        review=json.loads(path.read_text(encoding='utf-8'));review['answers'].pop();dump(path,review)
    with pytest.raises(ValueError):
        read_reviews(directory,[path,path] if change=='duplicate' else [path])


def test_developer_factual_correction_is_separate_and_cannot_invent_pass(tmp_path):
    directory,path,packet=fixture(tmp_path,2)
    original=path.read_text(encoding='utf-8')
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    aid=next(e['answerId'] for e in manifest['schedule'] if e['arm']=='selective')
    dp=directory/'developer-review.json';dev=json.loads(dp.read_text(encoding='utf-8'))
    dev['disputedReviewChecks']=[{'answerId':aid,'checkId':'accuracy','proposedStatus':'fail',
        'quote':'Check','basis':'synthetic contrary fact','reason':'independent reviewer missed this'}]
    dump(dp,dev)
    result=summarize(directory,[path])
    assert not result['expansionGateMet']
    assert len(result['conservativeDeveloperCorrections'])==1
    assert path.read_text(encoding='utf-8')==original
    dev['disputedReviewChecks'][0]['quote']='not present';dump(dp,dev)
    with pytest.raises(ValueError,match='UNSUPPORTED_DEVELOPER'): summarize(directory,[path])


def test_split_review_packet_keeps_null_service_records_in_hash(tmp_path):
    directory,path,packet=fixture(tmp_path,fail_first=True)
    original=json.loads(path.read_text(encoding='utf-8'))
    parts=[]
    for n,items in enumerate((packet['items'][:6],packet['items'][6:]),1):
        part={**packet,'items':items};ids={i['answerId'] for i in items}
        dump(directory/f'blind-part-{n}.json',part)
        review={**original,'packetHash':digest(part),'answers':[a for a in original['answers'] if a['answerId'] in ids]}
        rp=directory/f'review-{n}.json';dump(rp,review);parts.append(rp)
    _,rows,checked=read_reviews(directory,parts)
    assert len(checked)==len(rows)-1
    altered=json.loads((directory/'blind-part-1.json').read_text(encoding='utf-8'))
    altered['items'][0]['materials']['code']['text']='tampered'
    dump(directory/'blind-part-1.json',altered)
    with pytest.raises(ValueError):read_reviews(directory,parts)



def test_failure_status_cannot_reuse_visible_success_for_comparison(tmp_path):
    directory,path,_=fixture(tmp_path)
    rp=directory/'record-001.json';record=json.loads(rp.read_text(encoding='utf-8'))
    record['status']='unavailable';dump(rp,record)
    with pytest.raises(ValueError,match='RESULT_STATUS'):read_reviews(directory,[path])


def test_reading_benefit_is_not_attributed_to_an_unread_skill(tmp_path):
    directory,path,_=fixture(tmp_path,2,selected_skill='array-and-hash')
    result=summarize(directory,[path])
    assert not result['perSkill']['tree-and-graph']['readingBenefitGateMet']
    assert result['perSkill']['array-and-hash']['readingBenefitGateMet']



def test_direct_answer_cannot_claim_it_loaded_a_skill(tmp_path):
    directory,path,_=fixture(tmp_path)
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    e=next(e for e in manifest['schedule'] if e['arm']=='selective')
    rp=directory/(e['recordId']+'.json');r=json.loads(rp.read_text(encoding='utf-8'))
    r['loaded']={'made-up':'data'};dump(rp,r)
    with pytest.raises(ValueError,match='LOADED_CONTENT'):read_reviews(directory,[path])
