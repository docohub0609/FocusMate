"""Run real-Docker local experiments; pass means observed infrastructure behavior matched."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from focusmate.adapters.tool_storage import SQLiteToolBackend
from focusmate.core.tool_gateway import ToolGateway
from services.oj_worker.local_runs import LocalRunService
from tools.tool_demo import business_snapshot, provision_fixture


CASES=[
 ('normal-snippet','x += 1\n',{'snippet':{'kind':'lines','startLine':1,'endLine':1},'initialVariables':{'x':2},'observe':{'identifiers':['x'],'returnValue':False}},lambda r:r.get('data',{}).get('observations')=={'x':3}),
 ('normal-function','def add(a,b):\n    return a+b\n',{'snippet':{'kind':'function','name':'add'},'initialVariables':{},'call':{'function':'add','args':[2,3]},'observe':{'identifiers':[],'returnValue':True}},lambda r:r.get('data',{}).get('returnValue')==5),
 ('mutation','values.append(3)\n',{'snippet':{'kind':'lines','startLine':1,'endLine':1},'initialVariables':{'values':[1,2]},'observe':{'identifiers':['values'],'returnValue':False}},lambda r:r.get('data',{}).get('observations')=={'values':[1,2,3]}),
 ('missing-dependency','x = missing + 1\n',{'snippet':{'kind':'lines','startLine':1,'endLine':1},'initialVariables':{},'observe':{'identifiers':['x'],'returnValue':False}},lambda r:r.get('status')=='failed' and r.get('data',{}).get('failure')=='LOCAL_RUN_RUNTIME_ERROR'),
 ('runtime-error','x = 1 / 0\n',{'snippet':{'kind':'lines','startLine':1,'endLine':1},'initialVariables':{},'observe':{'identifiers':['x'],'returnValue':False}},lambda r:r.get('status')=='failed' and r.get('data',{}).get('failure')=='LOCAL_RUN_RUNTIME_ERROR'),
 ('silent-loop','while True:\n    pass\n',{'snippet':{'kind':'lines','startLine':1,'endLine':2},'initialVariables':{},'observe':{'identifiers':[],'returnValue':False}},lambda r:r.get('status')=='failed' and r.get('data',{}).get('failure') in ('LOCAL_RUN_RESOURCE_LIMIT','LOCAL_RUN_TERMINATED_UNKNOWN')),
 ('output-limit','while True:\n    print("x"*10000)\n',{'snippet':{'kind':'lines','startLine':1,'endLine':2},'initialVariables':{},'observe':{'identifiers':[],'returnValue':False}},lambda r:r.get('status')=='failed' and r.get('data',{}).get('failure')=='LOCAL_RUN_RESOURCE_LIMIT'),
]


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():parser.error('output directory already exists')
    root=args.output;root.mkdir(parents=True);db=root/'business.sqlite3'
    base={'loopId':'fixture','goalContext':{'goalId':'g','scope':'subgoal','subgoalId':'sg'},
              'problemRef':{'problemId':'p','problemVersion':1},'allowedCodeVersions':[1],
              'allowedTools':['run_local_code'],'deadlineAt':(datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat()}
    provision_fixture(db,{'context':base,'fixtures':{'codes':[{'version':i+1,'source':case[1]} for i,case in enumerate(CASES)],'publicSamples':[]}})
    before=business_snapshot(db)
    local=LocalRunService(root/'local-runs')
    gateway=ToolGateway(SQLiteToolBackend(db,local_service=local),SQLiteToolLedger(root/'calls.sqlite3'))
    records=root/'records';records.mkdir();reports=[]
    for index,(name,source,arguments,check) in enumerate(CASES,1):
        context={**base,'loopId':'verify-'+str(index),'allowedCodeVersions':[index],
                     'deadlineAt':(datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat()}
        call_args={'codeVersion':index,'purpose':'验证 '+name,'observationIntent':'记录该场景的实际沙箱现象',**arguments}
        call={'callId':'call-'+str(index),'toolName':'run_local_code','arguments':call_args}
        result=gateway.invoke(context,call)
        (records/f'{index:02d}-{name}.json').write_text(json.dumps({'context':context,'call':call,'result':result},ensure_ascii=False,indent=2),encoding='utf-8')
        reports.append({'case':name,'passed':bool(check(result)),'toolStatus':result.get('status'),
                            'observedFailure':result.get('data',{}).get('failure') or result.get('errorCode')})
    unchanged=business_snapshot(db)==before
    output={'meaning':'passed仅表示真实Docker基础设施表现符合本脚本预期，不表示用户代码正确',
            'businessRecordsUnchanged':unchanged,'records':str(records.resolve()),
            'cases':reports,'passed':unchanged and all(x['passed'] for x in reports)}
    (root/'summary.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(output,ensure_ascii=False,indent=2))
    return 0 if output['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
