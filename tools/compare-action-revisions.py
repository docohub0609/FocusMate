"""Compare old prompts, revised prompts, then explicit prior-issue rechecking."""
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
from focusmate.core.action_planning import CANDIDATE_SCHEMA,REVIEW_SCHEMA,VERSION,REVIEW_VERSION
from action_cases import cases,inputs
from action_revision_cases import cases as new_cases
from action_revision_comparison import configurations
from focusmate.core.action_execution import ActionExecutor
from diagnosis_runtime import run_diagnosis


def save(path,value):
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--group',choices=['regression','new','minimalRegression','minimalNew'],required=True)
    p.add_argument('--baseline-manifest',type=Path,required=True)
    p.add_argument('--env-file',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True)
    a=p.parse_args();a.output_dir.mkdir(parents=True,exist_ok=False);load_local_env(a.env_file)
    old=json.loads(a.baseline_manifest.read_text(encoding='utf-8'))
    arms=configurations(old['actorPrompt'],old['reviewPrompt'])
    if a.group=='new':arms=arms[-1:]
    if a.group.startswith('minimal'):arms=[type('originalPromptsWithRecheck',(ActionExecutor,),{})]
    from action_revision_cases import final_cases
    fixtures=cases('holdout') if a.group in ('regression','minimalRegression') else final_cases() if a.group=='minimalNew' else new_cases()
    frozen=[(c,*inputs(c)) for c in fixtures]
    config=DeepSeekToolChatProvider.from_environment().public_config
    jobs=[(item,n,arm) for i,item in enumerate(frozen) for n in (1,2)
          for arm in arms[(i+n-1)%len(arms):]+arms[:(i+n-1)%len(arms)]]
    save(a.output_dir/'manifest.json',{'fixtures':fixtures,'group':a.group,
        'arms':[{'name':x.__name__,'actorPrompt':x.actor_prompt,'reviewPrompt':x.review_prompt,'trackIssues':x.track_review_issues} for x in arms],
        'modelConfig':config,'candidateSchema':CANDIDATE_SCHEMA,'reviewSchema':REVIEW_SCHEMA,
        'versions':[VERSION,REVIEW_VERSION],'order':[(x[0]['id'],n,arm.__name__) for x,n,arm in jobs],
        'source':'developerFixturesWithSourceCheckedExpectations','diagnosisSource':'developerFixtureNotRealT071',
        'baselineScope':'old prompts on same current executor/schema; not replay of historical service',
        'contentReview':'pendingDeveloperSourceReview','cost':'unknown'})
    def run(item,n,arm):
        case,bundle,diagnosis=item;d=a.output_dir/f'{case["id"]}-r{n}-{arm.__name__}';d.mkdir()
        save(d/'input.json',{'bundle':bundle,'diagnosis':diagnosis})
        start=time.monotonic();record={'caseId':case['id'],'repetition':n,'arm':arm.__name__}
        try:
            state=run_diagnosis(deepcopy(bundle),DeepSeekToolChatProvider.from_environment(),d,skills=[],
                                plan_action=True,diagnosis=deepcopy(diagnosis),action_executor=arm)
            save(d/'trace.json',state);r=state['rounds'][state['currentRoundId']]
            record.update(status=r['status'],failure=r['failure'],counts=r['counts'],
                          answer=[v['content'] for v in state['outbox']],contentReview='pending')
        except Exception as exc:record.update(status='failed',failure=type(exc).__name__)
        record['seconds']=time.monotonic()-start;save(d/'result.json',record)
        print(json.dumps({k:v for k,v in record.items() if k!='answer'},ensure_ascii=False),flush=True)
        return record
    results=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(run,*j) for j in jobs]
        for f in as_completed(futures):results.append(f.result())
    save(a.output_dir/'results.json',results)


if __name__=='__main__':main()
