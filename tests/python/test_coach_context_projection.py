"""Keep source identity and exact values without sending audit copies to the model."""
from copy import deepcopy
import json
import pytest

from focusmate.core.context_builder import hash_content
from focusmate.core.dynamic_coach_execution import request_payload
from focusmate.core.dynamic_coach_roles import verification_bundle
from focusmate.core.process_verification import expand_public_cases
from focusmate.core.verification_facts import fact_catalog, resolve_fact_quote
from tests.python.test_coach_v3_flow import establish
from tests.python.test_dynamic_coach_manager import manager
from tests.python.test_dynamic_coach_loop import materials


def test_facts_are_presented_once_but_resolve_to_the_original_fields(tmp_path):
    m=manager(tmp_path,workflow_version=3);key=establish(m)
    m.command('s','check','check',{'subgoalIds':[key]})
    material=materials(m)
    content={'codeSnapshotRef':'code:1','completed':True,'sampleStatus':'passed','caseResults':[
        {'index':1,'stdin':'2 0 2 1 1 0\n','expectedOutput':'0 0 1 1 2 2\n',
         'stdout':'0 0 1 1 2 2\n','outcome':'passed'}]}
    material['records'].append({'ref':'run:one','kind':'runEvidence','version':1,
        'content':content,'contentHash':hash_content(content),'goalContext':m.read('s')['goalContext']})
    st=m.claim('s','w',materials=material);state=m.read('s');frozen=deepcopy(st)
    payload=request_payload(state,st)
    assert all('evidenceText' not in x['content'] for x in payload['materials'])
    assert all('value' not in x for x in payload['factCatalog'])
    quote_schema=payload['outputSchema']['properties']['checks']['items']['properties']['quotes']['items']
    assert all('enum' not in option['properties'].get('factId',{}) for option in quote_schema['anyOf'])
    bundle=verification_bundle(state,state['rounds'][st['roundId']],st)
    facts={f['factId']:f for f in fact_catalog({**bundle,'records':expand_public_cases(bundle['records'])})}
    lookup={r['ref']:r for r in payload['materials']}
    for item in payload['factCatalog']:
        original=facts[item['factId']]
        assert item=={k:original[k] for k in ('factId','ref','path')}
        field=lookup[item['ref']]['content']
        for key in item['path'].split('/')[1:]:
            key=key.replace('~1','/').replace('~0','~')
            field=field[int(key)] if isinstance(field,list) else field[key]
        assert field==original['value']
        assert json.loads(resolve_fact_quote({'factId':item['factId']},facts)['text'])[item['path']]==field
    assert st==frozen
    with pytest.raises(ValueError,match='INVALID_FACT_REFERENCE'):
        resolve_fact_quote({'factId':'fact-invented'},facts)


def test_repair_sends_original_candidate_once_without_archived_context(tmp_path):
    from tests.python.test_coach_repair_agent import new_manager,repair_step,RepairProvider
    from tests.python.test_coach_compact_flow import compact
    m=new_manager(tmp_path);m.command('s','ask','ask',{'text':'解释循环'})
    st=m.claim('s','w',materials=materials(m))
    candidate={'kind':'plan','value':compact(), 'contextSnapshot':{'payload':'archived-only-'*10000},
               'usage':{'promptTokens':10000},'modelConfig':{'model':'fixture'}}
    failure={'kind':'failure','code':'INVALID_ACTION_REFERENCE','correctable':True,
             'formatError':'invalid evidence reference','rejectedResult':candidate}
    m.complete('s',st['stepId'],st['token'],failure)
    original=deepcopy(m.read('s')['rounds'][st['roundId']]['repairRecord'])
    provider=RepairProvider(compact());repair_step(m,provider)
    packet=json.loads(provider.calls[0]['messages'][-1]['content'])
    assert 'archived-only-' not in json.dumps(packet)
    assert packet['original']=={'kind':'plan','value':compact()}
    assert 'rejectedResult' not in packet['failure']
    assert packet['failure']['formatError']==failure['formatError']
    archived=m.read('s')['rounds'][st['roundId']]['repairRecord']
    assert archived['failure']==original['failure']==failure
    assert archived['original']==original['original']
    assert 'archived-only-' in json.dumps(archived['failure'])


def test_frozen_repair_packet_cannot_change_or_restore_hidden_audit(tmp_path):
    from tests.python.test_coach_repair_agent import new_manager,claim_failure
    m=new_manager(tmp_path);m.command('s','ask','ask',{'text':'解释循环'})
    claim_failure(m);st=m.claim('s','repair-worker',materials=materials(m))
    record=deepcopy(m.read('s')['rounds'][st['roundId']]['repairRecord'])
    record['modelPacket']['materials']['other']={'contextSnapshot':'different materials'}
    record['modelPacketHash']=hash_content(record['modelPacket'])
    with pytest.raises(ValueError,match='REPAIR_SCOPE_VIOLATION'):
        m.repair_checkpoint('s',st['stepId'],st['token'],record)


def test_repair_preserves_audit_named_fields_inside_the_model_candidate():
    from focusmate.core.coach_model_projection import repair_packet
    candidate={'contextSnapshot':{'unexpected':'model supplied this field'},
               'modelConfig':{'model':'also an invalid candidate field'},
               'usage':{'tokens':'must remain available for correction'}}
    rejected={'kind':'plan','value':deepcopy(candidate),
              'contextSnapshot':{'payload':'server audit copy'},'modelConfig':{'model':'server'}}
    record={'failure':{'kind':'failure','code':'INVALID_ACTION_FORMAT','rejectedResult':deepcopy(rejected)},
            'original':deepcopy(rejected),'materials':{'request':{'outputSchema':{}}}}
    frozen=deepcopy(record)
    packet=repair_packet(record)
    assert packet['original']=={'kind':'plan','value':candidate}
    assert record==frozen


def test_read_material_uses_frozen_projected_packet_and_checks_its_hash():
    from focusmate.core.repair_agent import RepairExecutor,RepairValidation
    from tests.python.test_repair_agent import record,Provider,call,SCHEMA
    value=record()
    value['modelPacket']={'failure':value['failure'],'materials':{'input-1':{'error':'projected only'}},'original':value['original']}
    value['modelPacketHash']=hash_content(value['modelPacket'])
    provider=Provider(call('read_material',{'materialIds':['input-1']}),call())
    result=RepairExecutor(provider,clock=lambda:1).execute(value,candidate_schema=SCHEMA,
        validate=lambda *_:RepairValidation(True),checkpoint=lambda _:None,guard=lambda:None,deadline=10)
    assert result['status']=='succeeded'
    assert json.loads(provider.calls[1]['messages'][-1]['content'])=={'input-1':{'error':'projected only'}}
    value['modelPacket']['materials']['input-1']['error']='changed'
    provider=Provider()
    result=RepairExecutor(provider,clock=lambda:1).execute(value,candidate_schema=SCHEMA,
        validate=lambda *_:pytest.fail('changed packet accepted'),checkpoint=lambda _:None,guard=lambda:None,deadline=10)
    assert result['reason']=='REPAIR_PRESENTATION_MISMATCH' and not provider.calls
