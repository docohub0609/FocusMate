"""T070 real model paths and real Docker with explicitly simulated semantic roles."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src'),str(ROOT/'tools')]
from goal_compilation_examples import seed_example
from apps.api.submission_storage import SubmissionRepository
from apps.api.deepseek_provider import load_local_env
from focusmate.adapters.tool_chat import DeepSeekToolChatProvider
from focusmate.adapters.tool_storage import SQLiteToolBackend
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from focusmate.adapters.manager_storage import SQLiteManagerStore
from focusmate.adapters.manager_runs import ManagerRunLookup
from focusmate.core.session_manager import SessionManager
from focusmate.core.manager_execution import ManagerExecutor, advance_once, reconcile_runs
from focusmate.core.tool_gateway import ToolGateway
from services.oj_worker.tool_public_runs import PublicRunService


def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


class Scripted:
    public_config={'provider':'Fake','maxTokens':100,'source':'developerFixture'}
    def complete(self,**kwargs):
        if any(x['role']=='tool' for x in kwargs['messages']):return {'content':'这是开发模拟解释，实际运行结果见工具记录。','toolCalls':[]}
        value=json.loads(kwargs['messages'][1]['content']);version=value['currentCode']['codeVersion']
        return {'content':'','toolCalls':[{'callId':'run-one','toolName':'run_public_samples','arguments':{'codeVersion':version}}]}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--env-file',type=Path,required=True);a=p.parse_args();out=a.output_dir;out.mkdir(parents=True,exist_ok=False)
    business=out/'business.sqlite3';goal,frozen,_=seed_example(business,'two-sum');repo=SubmissionRepository(business)
    with sqlite3.connect(business) as db:
        db.execute('CREATE TABLE public_test_cases(test_case_id TEXT,asset_id TEXT,asset_version INTEGER,problem_id TEXT,problem_version INTEGER,example_index INTEGER,stdin_text TEXT,expected_output_text TEXT)')
        for i,e in enumerate(frozen['problemPackage']['examples']):
            db.execute('INSERT INTO public_test_cases VALUES(?,?,?,?,?,?,?,?)',(f'sample-{i}','public-1',1,goal['problemRef']['problemId'],1,i,e['input'],e['output']))
    source='n,target=map(int,input().split())\na=list(map(int,input().split()))\nseen={}\nfor j,x in enumerate(a):\n    if target-x in seen:\n        print(seen[target-x],j)\n        break\n    seen[x]=j\n'
    code=repo.create_code_snapshot(goal['goalId'],source,'python3')
    slow=repo.create_code_snapshot(goal['goalId'],'import time\ntime.sleep(1)\n'+source,'python3')
    public=PublicRunService(business,out/'public');backend=SQLiteToolBackend(business,public)
    gateway=ToolGateway(backend,SQLiteToolLedger(out/'tool-calls.sqlite3'))
    manager=SessionManager(SQLiteManagerStore(out/'manager.sqlite3'));lookup=ManagerRunLookup(public,None)
    load_local_env(a.env_file);provider=DeepSeekToolChatProvider.from_environment()
    real=ManagerExecutor(provider,gateway,backend,materials={'problem':frozen['problemPackage']})
    fake=ManagerExecutor(Scripted(),gateway,backend,fixture_roles=True)
    summary=[]
    def create(name,version,allowed,text):
        manager.create(name,code['goalContext'],goal['problemRef'],contract_version=1,code_version=version,allowed_tools=allowed)
        manager.command(name,'request','ask',{'text':text})
    for name,allowed,text in [('real-direct',[],'只解释 seen 字典在这份代码中保存什么，不运行，不给完整解法。'),
                              ('real-public',['run_public_samples'],'请实际运行已授权的当前代码公开样例，再根据真实结果简短说明，不提供完整代码。')]:
        started=time.monotonic();create(name,code['codeVersion'],allowed,text)
        for _ in range(12):
            tick=advance_once(manager,name,real,lookup=lookup)
            if tick['status']=='idle':break
        state=manager.read(name);r=state['rounds'][state['currentRoundId']]
        save(out/(name+'.json'),{'source':'real DeepSeek; real tools; no production checker','state':state})
        summary.append({'scenario':name,'status':r['status'],'failure':r['failure'],'candidateSaved':bool(r['candidate']),
                        'published':bool(state['outbox']),'seconds':time.monotonic()-started,
                        'modelRequests':sum('visibleResponse' in (s.get('result') or {}) for s in state['steps'].values())})
        save(out/'summary.json',summary)
    for name,operation in [('pause-during-run','pause'),('message-during-run','message'),('cancel-during-run','stop_run')]:
        started=time.monotonic();create(name,slow['codeVersion'],['run_public_samples'],'开发模拟请求运行')
        advance_once(manager,name,fake)
        observed=False
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(advance_once,manager,name,fake)
            for _ in range(120):
                state=manager.read(name);r=state['currentRoundId']
                with sqlite3.connect(public.path) as db:
                    row=db.execute("SELECT container_name FROM tool_public_tasks WHERE json_extract(context_json,'$.loopId')=?",(r,)).fetchone()
                if row and row[0]:
                    check=subprocess.run(['docker','inspect','--format','{{.State.Running}}',row[0]],capture_output=True,text=True)
                    if check.returncode==0 and check.stdout.strip()=='true':observed=True;break
                if future.done():break
                time.sleep(.1)
            payload={'text':'补充：请只报告实际运行结果。'} if operation=='message' else {}
            manager.command(name,'control',operation,payload)
            if operation=='stop_run':reconcile_runs(manager,name,lookup)
            result=future.result(timeout=40)
        state=manager.read(name)
        save(out/(name+'.json'),{'source':'Fake semantic roles + real Docker','observedRunningContainerBeforeControl':observed,'stepResult':result,'state':state})
        summary.append({'scenario':name,'observedRunningContainerBeforeControl':observed,'sessionStatus':state['status'],
                        'published':bool(state['outbox']),'seconds':time.monotonic()-started})
        save(out/'summary.json',summary)
    name='recover-lost-receipt';create(name,code['codeVersion'],['run_public_samples'],'开发模拟运行，丢失回调')
    advance_once(manager,name,fake);step=manager.claim(name,'lost-worker')
    physical=fake.execute(manager.read(name),step)  # Deliberately do not commit this callback.
    restarted=SessionManager(SQLiteManagerStore(out/'manager.sqlite3'))
    receipt=reconcile_runs(restarted,name,lookup);again=reconcile_runs(restarted,name,lookup)
    state=restarted.read(name)
    save(out/(name+'.json'),{'source':'Fake roles, real Docker; lost callback simulated, fresh manager connection','actualResult':physical,'recovery':receipt,'repeatRecovery':again,'state':state})
    summary.append({'scenario':name,'receipts':len(receipt['receipts']),'repeatReceipts':len(again['receipts']),
                    'toolSteps':sum(s['kind']=='tool' for s in state['steps'].values()),'published':bool(state['outbox'])})
    save(out/'summary.json',{'notice':'Developer scenarios, not real learner outcomes. First two use real DeepSeek; remaining semantic roles are scripted. No T071/T072/T073 acceptance.',
        'modelConfig':provider.public_config,'cost':'unknown','scenarios':summary})
    print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':main()
