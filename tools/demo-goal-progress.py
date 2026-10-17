"""Create real save/run/organize records in isolated databases, then read them with T063."""
import argparse
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src'),str(ROOT/'tools')]
from goal_compilation_examples import seed_example
from apps.api.submission_storage import SubmissionRepository
from services.oj_worker.tool_public_runs import PublicRunService
from focusmate.adapters.observations import SQLiteObservationStore
from focusmate.core.event_organizer import EventOrganizer
from focusmate.core.goal_template import build_generic_algorithm_goal_contract
from focusmate.adapters.progress_storage import SQLiteProgressResolver
from focusmate.adapters.progress_estimation import DeepSeekProgressEstimationProvider
from focusmate.core.progress_estimation import estimate_progress
from apps.api.deepseek_provider import load_local_env
from progress_evaluation import save


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--env-file',type=Path,required=True)
    args=p.parse_args();out=args.output_dir;out.mkdir(parents=True,exist_ok=False)
    dbpath=out/'business.sqlite3';goal,frozen,_=seed_example(dbpath,'two-sum');repo=SubmissionRepository(dbpath)
    contract=build_generic_algorithm_goal_contract(goal,created_at=goal['createdAt'])
    contract.update(contractStatus='frozen',objective='两数之和：返回两个不同位置，其数值之和等于target。')
    # A labelled development contract seed, not a claim that T042 reviewed this draft.
    with sqlite3.connect(dbpath) as db:
        db.execute('CREATE TABLE goal_contract_versions(goal_id TEXT,version INTEGER,status TEXT,compilation_json TEXT)')
        db.execute('INSERT INTO goal_contract_versions VALUES(?,?,?,?)',(goal['goalId'],1,'frozen',json.dumps({'contract':contract})))
        db.execute('CREATE TABLE public_test_cases(test_case_id TEXT,asset_id TEXT,asset_version INTEGER,problem_id TEXT,problem_version INTEGER,example_index INTEGER,stdin_text TEXT,expected_output_text TEXT)')
        for i,e in enumerate(frozen['problemPackage']['examples']):
            db.execute('INSERT INTO public_test_cases VALUES(?,?,?,?,?,?,?,?)',(f'example-{i}','public-01',1,goal['problemRef']['problemId'],1,i,e['input'],e['output']))
    old=repo.create_code_snapshot(goal['goalId'],'n,target=map(int,input().split())\na=list(map(int,input().split()))\nprint(0,0)\n','python3')
    new=repo.create_code_snapshot(goal['goalId'],
        'n,target=map(int,input().split())\na=list(map(int,input().split()))\nseen={}\nfor j,x in enumerate(a):\n    if target-x in seen:\n        print(seen[target-x],j)\n        break\n    seen[x]=j\n','python3')
    service=PublicRunService(dbpath,out/'work');runs=[]
    for i,code in enumerate([old,new]):
        context={'loopId':f't063-demo-{i}','goalContext':code['goalContext'],'problemRef':goal['problemRef'],
            'deadlineAt':(datetime.now(timezone.utc)+timedelta(seconds=90)).isoformat()}
        runs.append(service.run(context,code))
    store=SQLiteObservationStore(dbpath,public_task_db=service.path)
    organized=EventOrganizer(store).process(goal['goalId'],now=datetime.now(timezone.utc)+timedelta(seconds=3))
    observations=store.list_observations(goal['goalId'])
    req={'schemaVersion':'1.0.0','goalContext':new['goalContext'],'problemRef':goal['problemRef'],
        'contractVersion':1,'contractRef':'g01','currentCodeRef':'c02','baselineRefs':['c01'],
        'sources':[],'budget':{'contextWindowTokens':32000,'reservedOutputTokens':8192,'safetyMarginTokens':1000}}
    def source(ref,kind,typ,identifier,version=1):
        req['sources'].append({'ref':ref,'kind':kind,'sourceType':typ,'sourceId':identifier,'version':version,'contentHash':'sha256:'+'0'*64})
    source('g01','goal','contract',goal['goalId']);source('p01','problemFact','problem',goal['problemRef']['problemId'])
    for i,code in enumerate([old,new],1):source(f'c{i:02}','code','code',code['codeSnapshotId'],code['codeVersion'])
    for i,run in enumerate(runs,1):
        if run.get('runId'):source(f'r{i:02}','runEvidence','publicRun',run['runId'])
    for i,o in enumerate(observations,1):source(f'o{i:02}','observation','observation',o['observationId'])
    with SQLiteProgressResolver(dbpath,req,public_task_db=service.path) as resolver:
        for s in req['sources']:s['contentHash']=resolver.resolve(s['ref'])['contentHash']
    save(out/'request.json',{'request':req,'supplementalRecords':[]})
    before=dbpath.read_bytes();public_before=service.path.read_bytes()
    load_local_env(args.env_file);provider=DeepSeekProgressEstimationProvider.from_environment()
    with SQLiteProgressResolver(dbpath,req,public_task_db=service.path) as resolver:report=estimate_progress(req,resolver,provider)
    save(out/'report.json',report)
    save(out/'operations.json',{'sourceNotice':'Developer-created scenario. Contract seeded without T042 review; saves, Docker public runs and T061 organization are real. No frontend autosave or learner session.',
        'runs':runs,'organization':organized,'observations':observations,'organizationFailures':store.list_failures(goal['goalId']),
        'readOnlyChecks':{'businessUnchanged':before==dbpath.read_bytes(),'publicUnchanged':public_before==service.path.read_bytes()},
        'analysisStatus':report['status'],'cost':'unknown'})
    print(json.dumps({'status':report['status'],'failure':report.get('failure'),'runs':[r.get('sampleStatus') for r in runs]},ensure_ascii=False))


if __name__=='__main__':main()
