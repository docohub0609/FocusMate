"""Real provider evaluation; developer expectations never enter the executor."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src'),str(ROOT/'tools')]
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.manager_storage import SQLiteManagerStore
from focusmate.adapters.tool_chat import DeepSeekToolChatProvider
from focusmate.adapters.tool_storage import SQLiteToolBackend
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from focusmate.adapters.manager_runs import ManagerRunLookup
from focusmate.core.session_manager import SessionManager
from focusmate.core.process_verification import prepare_verification, VERSION, PROMPT
from focusmate.core.process_execution import ProcessExecutor
from focusmate.core.context_builder import MapContextResolver,hash_content
from focusmate.core.manager_execution import advance_once
from focusmate.core.tool_gateway import ToolGateway
from process_cases import development_cases,fresh_cases,materials
from tool_demo import provision_fixture,business_snapshot


def save(path,value):
    with Path(path).open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)


def run_case(case,provider,directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    req,records,action=materials(case)
    bundle=prepare_verification(req,MapContextResolver(records),action)
    save(directory/'input.json',bundle);save(directory/'expectation.json',{'expected':case['expected'],'source':'developer-reviewed-fixture-not-user-confirmation'})
    manager=SessionManager(SQLiteManagerStore(directory/'manager.sqlite3'))
    allowed=case.get('tools',[]);code=1 if req['currentCodeRef'] else None
    manager.create('s',{'goalId':'g1','scope':'goal'},req['problemRef'],contract_version=1,code_version=code,allowed_tools=allowed,mode='coaching')
    # Explicit development fixture: this is not a model-reviewed real-user Action.
    def seed(state):
        state['outbox'].append({'actionId':'a1','learningAction':deepcopy(action),'source':'developerFixture','content':action['content']})
        return {'fixture':True}
    manager.store.change('s','fixture-action',{'kind':'developerFixture'},seed)
    gateway=backend=lookup=None;before=None
    if allowed:
        from services.oj_worker.tool_public_runs import PublicRunService
        from services.oj_worker.local_runs import LocalRunService
        db=directory/'business.sqlite3'
        provision_fixture(db,{'context':{'goalContext':{'goalId':'g1','scope':'goal'},'problemRef':req['problemRef']},
            'fixtures':{'codes':[{'version':1,'source':case['code']}],'publicSamples':case.get('samples',[])}})
        before=business_snapshot(db)
        public=PublicRunService(db,directory/'public') if 'run_public_samples' in allowed else None
        local=LocalRunService(directory/'local') if 'run_local_code' in allowed else None
        backend=SQLiteToolBackend(db,public,local);gateway=ToolGateway(backend,SQLiteToolLedger(directory/'calls.sqlite3'))
        lookup=ManagerRunLookup(public,local)
    manager.command('s','verification','verification',{'actionId':'a1','subgoalId':'s1','materialRefs':req['materialRefs'],'codeVersion':code})
    executor=ProcessExecutor(provider,gateway,backend,bundle=bundle);started=time.monotonic()
    while True:
        tick=advance_once(manager,'s',executor,lookup=lookup)
        if tick['status'] in ('idle','not_applied'):break
    state=manager.read('s');r=state['rounds'][state['currentRoundId']]
    result={'round':r,'seconds':time.monotonic()-started,'businessUnchanged':before is None or before==business_snapshot(db),
            'contentReview':'pendingDeveloperReview','origin':'developerScenariosWithRealProviderAndTools'}
    save(directory/'trace.json',state);save(directory/'result.json',result)
    return {'directory':str(directory),'status':r['status'],'failure':r['failure'],
            'verdict':r.get('verification',{}).get('verifierResult',{}).get('status'),'calls':r['counts'],'seconds':result['seconds']}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--group',choices=['development','fresh'],required=True)
    p.add_argument('--env-file',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--case',type=int,action='append');a=p.parse_args()
    a.output_dir.mkdir(parents=True,exist_ok=False);load_local_env(a.env_file);provider=DeepSeekToolChatProvider.from_environment()
    cases=development_cases() if a.group=='development' else fresh_cases()
    jobs=[(i,c,n) for i,c in enumerate(cases,1) if not a.case or i in a.case for n in (1,2)]
    save(a.output_dir/'manifest.json',{'group':a.group,'cases':cases,'caseHash':hash_content(cases),'promptVersion':VERSION,
        'prompt':PROMPT,'modelConfig':provider.public_config,'jobs':[[i,n] for i,c,n in jobs],
        'notice':'预期不发给模型；数字匹配不替代Codex内容复核；没有自动重抽。'})
    summaries=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures={pool.submit(run_case,c,provider,a.output_dir/f'{i:02d}-{n}'):(i,n) for i,c,n in jobs}
        for f in as_completed(futures):
            i,n=futures[f]
            try:result=f.result()
            except Exception as exc:result={'status':'executionError','errorType':type(exc).__name__}
            summaries.append({'case':i,'repetition':n,**result});print(json.dumps(summaries[-1],ensure_ascii=False),flush=True)
    save(a.output_dir/'summary.json',summaries)


if __name__=='__main__':main()
