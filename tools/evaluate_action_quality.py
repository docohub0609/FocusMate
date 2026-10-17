"""Controlled review / development / fresh-flow experiments. Not production routing."""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src'),str(ROOT/'tools')]
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.tool_chat import DeepSeekToolChatProvider
from focusmate.adapters.action_review import DeepSeekActionReviewProvider
from focusmate.core.action_execution import ActionExecutor
from focusmate.core.action_quality import FocusedActionExecutor,ACTOR_PROMPT,REPAIR_PROMPT,review_instructions
from focusmate.core.action_planning import validate_action_review
from focusmate.contracts.skill_files import strict_json
from action_quality_cases import reviewer_cases,review_payload,repair_cases
from action_cases import cases,inputs
from diagnosis_runtime import run_diagnosis


def save(path,value):
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)


def run_fixed(case,n,thinking,directory):
    label='thinking' if thinking else 'plain';d=directory/f'{case["id"]}-r{n}-{label}';d.mkdir()
    payload=review_payload(case);prompt=review_instructions([]);start=time.monotonic()
    record={'caseId':case['id'],'repetition':n,'arm':label}
    save(d/'request.json',{'system':prompt,'payload':payload})
    try:
        p=DeepSeekActionReviewProvider.from_environment(thinking=thinking)
        response=p.complete(messages=[{'role':'system','content':prompt},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}],tools=[],deadline=start+120)
        save(d/'response.json',response)
        if response['toolCalls']:raise ValueError('REVIEW_TOOL_FORBIDDEN')
        value=strict_json(response['content'])
        record.update(status=validate_action_review(value,case['candidate'],payload['evidenceCatalog']),modelConfig=p.public_config)
        record['matchesExpected']=record['status']==case['expected']
    except Exception as e:record.update(status='notCompleted',failure=getattr(e,'code',type(e).__name__),matchesExpected=False)
    record['seconds']=time.monotonic()-start;save(d/'result.json',record);return record


def run_flow(item,n,arm,directory):
    case,bundle,diagnosis=item;d=directory/f'{case["id"]}-r{n}-{arm}';d.mkdir()
    save(d/'input.json',{'bundle':bundle,'diagnosis':diagnosis});start=time.monotonic()
    record={'caseId':case['id'],'repetition':n,'arm':arm}
    try:
        actor=DeepSeekToolChatProvider.from_environment()
        rp=DeepSeekActionReviewProvider.from_environment(thinking=True) if arm=='candidate' else None
        state=run_diagnosis(deepcopy(bundle),actor,d,skills=[],plan_action=True,diagnosis=deepcopy(diagnosis),
                            action_executor=FocusedActionExecutor if arm=='candidate' else ActionExecutor,review_provider=rp)
        save(d/'trace.json',state);r=state['rounds'][state['currentRoundId']]
        record.update(status=r['status'],failure=r['failure'],counts=r['counts'],answer=[x['content'] for x in state['outbox']],contentReview='pending')
    except Exception as e:record.update(status='notCompleted',failure=getattr(e,'code',type(e).__name__))
    record['seconds']=time.monotonic()-start;save(d/'result.json',record);return record


def run_repair(case,n,arm,directory):
    d=directory/f'{case["id"]}-r{n}-{arm}';d.mkdir();start=time.monotonic()
    record={'caseId':case['id'],'repetition':n,'arm':arm,'contentReview':'pending'}
    actor=DeepSeekToolChatProvider.from_environment()
    cls=FocusedActionExecutor if arm=='candidate' else ActionExecutor
    executor=cls(actor,None,None,bundle=case['bundle'],skills=[],diagnosis=case['diagnosis'])
    material={'materials':deepcopy(case['bundle']['records']),'diagnosis':deepcopy(case['diagnosis']),
              'evidenceCatalog':[r['ref'] for r in case['bundle']['records']],'publishedActions':[]}
    prompt,payload=executor.repair_request(material,case['candidate'],case['firstReview'],case['previousIssues'])
    save(d/'request.json',{'system':prompt,'payload':payload})
    try:
        response=actor.complete(messages=[{'role':'system','content':prompt},{'role':'user','content':json.dumps(payload,ensure_ascii=False)}],tools=[],deadline=start+120)
        save(d/'response.json',response)
        if response['toolCalls']:raise ValueError('REPAIR_TOOL_FORBIDDEN')
        from focusmate.core.action_planning import validate_action_candidate
        candidate=validate_action_candidate(strict_json(response['content']),case['bundle'],case['diagnosis'])
        # Common second reviewer isolates the effect of the repair prompt.
        checker=FocusedActionExecutor(actor,None,None,bundle=case['bundle'],skills=[],diagnosis=case['diagnosis'])
        rp,rbody=checker.review_request(material,candidate,case['previousIssues'])
        save(d/'review-request.json',{'system':rp,'payload':rbody})
        reviewed=DeepSeekActionReviewProvider.from_environment(thinking=True).complete(messages=[{'role':'system','content':rp},
            {'role':'user','content':json.dumps(rbody,ensure_ascii=False)}],tools=[],deadline=start+120)
        save(d/'review-response.json',reviewed)
        if reviewed['toolCalls']:raise ValueError('REVIEW_TOOL_FORBIDDEN')
        record.update(status=validate_action_review(strict_json(reviewed['content']),candidate,material['evidenceCatalog'],previous_issues=case['previousIssues']),answer=[candidate['content']])
    except Exception as e:record.update(status='notCompleted',failure=getattr(e,'code',str(e)))
    record['seconds']=time.monotonic()-start;save(d/'result.json',record);return record


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--group',choices=['review','repair','development','fresh'],required=True)
    p.add_argument('--env-file',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--case',action='append',dest='case_ids',help='Explicit development rerun subset; recorded in manifest.')
    p.add_argument('--arm',choices=['baseline','candidate'])
    p.add_argument('--replay-dir',type=Path,help='Reuse exact frozen development inputs, including diagnosis identifiers.')
    a=p.parse_args();a.output_dir.mkdir(parents=True,exist_ok=False);load_local_env(a.env_file)
    if a.group=='review':
        fixtures=reviewer_cases();jobs=[(c,n,b) for i,c in enumerate(fixtures) for n in (1,2) for b in ([False,True] if (i+n)%2 else [True,False])]
        worker=run_fixed
    elif a.group=='repair':
        fixtures=repair_cases();jobs=[(c,n,arm) for i,c in enumerate(fixtures) for n in (1,2)
            for arm in (['baseline','candidate'] if (i+n)%2 else ['candidate','baseline'])]
        worker=run_repair
    else:
        if a.group=='development':fixtures=cases('holdout')
        else:
            from action_quality_fresh import cases as fresh_cases
            fixtures=fresh_cases()
        if a.replay_dir:
            frozen=[]
            for c in fixtures:
                saved=strict_json((a.replay_dir/f'{c["id"]}-r1-baseline'/'input.json').read_text(encoding='utf-8'))
                frozen.append((c,saved['bundle'],saved['diagnosis']))
        else:frozen=[(c,*inputs(c)) for c in fixtures]
        jobs=[(item,n,arm) for i,item in enumerate(frozen) for n in (1,2) for arm in (['baseline','candidate'] if (i+n)%2 else ['candidate','baseline'])]
        worker=run_flow
    if a.case_ids:
        known={c['id'] for c in fixtures}
        if not set(a.case_ids)<=known:raise ValueError('UNKNOWN_CASE_SELECTION')
        jobs=[j for j in jobs if (j[0][0]['id'] if isinstance(j[0],tuple) else j[0]['id']) in a.case_ids]
    if a.arm:
        if a.group=='review':raise ValueError('ARM_NOT_SUPPORTED_FOR_REVIEW')
        jobs=[j for j in jobs if j[2]==a.arm]
    save(a.output_dir/'manifest.json',{'group':a.group,'fixtures':fixtures,'source':'developerFixturesNotLiveUsers',
        'actorPrompt':ACTOR_PROMPT,'reviewPrompt':review_instructions([]),'repairPrompt':REPAIR_PROMPT,'recheckPrompt':review_instructions([{}]),
        'baselineActor':ActionExecutor.actor_prompt,'baselineReview':ActionExecutor.review_prompt,
        'modelConfig':DeepSeekToolChatProvider.from_environment().public_config,
        'reviewPlain':DeepSeekActionReviewProvider.from_environment(thinking=False).public_config,
        'reviewThinking':DeepSeekActionReviewProvider.from_environment(thinking=True).public_config,
        'sourceReview':'pendingMainCodex','repetitions':2,'cost':'unknown','caseSelection':a.case_ids,'armSelection':a.arm,
        'replayDirectory':str(a.replay_dir) if a.replay_dir else None,
        'adoptionGate':'both single-trial and both-trials success higher on fresh cases; error release/false rejection no increase; same deadline',
        'order':[(x[0]['id'] if isinstance(x,tuple) else x['id'],n,b) for x,n,b in jobs]})
    records=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(worker,*j,a.output_dir) for j in jobs]
        for f in as_completed(futures):
            r=f.result();records.append(r);print(json.dumps({k:v for k,v in r.items() if k!='answer'},ensure_ascii=False),flush=True)
    save(a.output_dir/'results.json',records)


if __name__=='__main__':main()
