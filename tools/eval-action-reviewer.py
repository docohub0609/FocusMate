"""Test the content reviewer with fixed answers, never expose the answer key."""
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
from focusmate.contracts.skill_files import strict_json
from focusmate.core.action_planning import REVIEW_PROMPT,REVIEW_SCHEMA,VERSION,REVIEW_VERSION,validate_action_review
from action_reviewer_cases import fixtures


def save(path,value):
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--env-file',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args()
    a.output_dir.mkdir(parents=True,exist_ok=False);load_local_env(a.env_file)
    frozen=fixtures()
    save(a.output_dir/'manifest.json',{'fixtures':frozen,'prompt':REVIEW_PROMPT,'version':VERSION,
        'reviewVersion':REVIEW_VERSION,'reviewSchema':REVIEW_SCHEMA,'repetitions':2,'source':'developerFixedAnswers','modelConfig':DeepSeekToolChatProvider.from_environment().public_config})
    def run(case,n):
        payload={'materials':case['bundle']['records'],'candidate':case['candidate'],'reviewSchema':REVIEW_SCHEMA,
                 'evidenceCatalog':[r['ref'] for r in case['bundle']['records']]}
        start=time.monotonic();record={'id':case['id'],'repetition':n,'request':payload}
        try:
            response=DeepSeekToolChatProvider.from_environment().complete(messages=[{'role':'system','content':REVIEW_PROMPT},
                {'role':'user','content':json.dumps(payload,ensure_ascii=False)}],tools=[],deadline=time.monotonic()+120)
            record['response']=response
            if response['toolCalls']:raise ValueError('REVIEW_TOOL_FORBIDDEN')
            record['verdict']=validate_action_review(strict_json(response['content']),case['candidate'],payload['evidenceCatalog'])
            record['matchesExpected']=record['verdict']==case['expected']
        except Exception as exc:record.update(failure=str(exc),matchesExpected=False)
        record['seconds']=time.monotonic()-start;save(a.output_dir/(case['id']+'-r'+str(n)+'.json'),record)
        return {k:v for k,v in record.items() if k not in ('request','response')}
    results=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks=[pool.submit(run,c,n) for c in frozen for n in (1,2)]
        for t in as_completed(tasks):
            record=t.result();results.append(record);print(json.dumps(record,ensure_ascii=False),flush=True)
    save(a.output_dir/'results.json',results)


if __name__=='__main__':main()
