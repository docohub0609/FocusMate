"""T072 developer evaluation; expected answers stay outside model requests."""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src'),str(ROOT/'tools')]
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.tool_chat import DeepSeekToolChatProvider
from focusmate.core.action_planning import VERSION,REVIEW_VERSION,ACTOR_PROMPT,REVIEW_PROMPT,CANDIDATE_SCHEMA,REVIEW_SCHEMA
from action_cases import cases,inputs
from diagnosis_runtime import run_diagnosis


def save(path,value):
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--group',choices=['development','holdout'],required=True)
    p.add_argument('--env-file',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--workers',type=int,choices=[1,2],default=2)
    a=p.parse_args();a.output_dir.mkdir(parents=True,exist_ok=False);load_local_env(a.env_file)
    fixtures=cases(a.group)
    frozen=[(case,*inputs(case)) for case in fixtures]
    config=DeepSeekToolChatProvider.from_environment().public_config
    save(a.output_dir/'manifest.json',{'source':'developerAuthoredAndSourceChecked','group':a.group,'fixtures':fixtures,
        'promptVersion':VERSION,'reviewVersion':REVIEW_VERSION,'actorPrompt':ACTOR_PROMPT,'reviewPrompt':REVIEW_PROMPT,'modelConfig':config,
        'candidateSchema':CANDIDATE_SCHEMA,'reviewSchema':REVIEW_SCHEMA,
        'diagnosisSource':'developerFixtureNotRealT071','repetitions':2,'contentReviewer':'pendingMainCodex'})
    def run(item,repetition):
        case,bundle,diagnosis=item;d=a.output_dir/(case['id']+'-r'+str(repetition));d.mkdir()
        save(d/'input.json',{'bundle':bundle,'diagnosis':diagnosis});start=time.monotonic()
        try:
            state=run_diagnosis(bundle,DeepSeekToolChatProvider.from_environment(),d,skills=[],plan_action=True,diagnosis=diagnosis)
            save(d/'trace.json',state);r=state['rounds'][state['currentRoundId']]
            record={'caseId':case['id'],'repetition':repetition,'status':r['status'],'failure':r['failure'],
                'counts':r['counts'],'answer':[v['content'] for v in state['outbox']],
                'contentReview':'pending','seconds':time.monotonic()-start}
        except Exception as exc:record={'caseId':case['id'],'repetition':repetition,'status':'failed','failure':type(exc).__name__,'seconds':time.monotonic()-start}
        save(d/'result.json',record);return record
    records=[]
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures=[pool.submit(run,item,n) for item in frozen for n in (1,2)]
        for f in as_completed(futures):
            result=f.result();records.append(result);print(json.dumps(result,ensure_ascii=False),flush=True)
    save(a.output_dir/'results.json',records)


if __name__=='__main__':main()
