"""Isolated real-model probe. Never opens the user's database."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from apps.api.coach_service import CoachService
from apps.api.deepseek_provider import load_local_env,DeepSeekConfig
from apps.api.problem_intake import create_problem_package
from apps.api.problem_features import build_human_feature_set
from apps.api.problem_storage import ProblemRepository
from focusmate.adapters.coach_chat import DeepSeekCoachProvider
from services.coach_worker.worker import CoachWorker


def seed(root,case):
    root.mkdir(parents=True,exist_ok=False)
    path=root/'business.sqlite3';problems=ProblemRepository(path)
    package=create_problem_package({k:case[k] for k in ('statement','inputSpec','outputSpec','constraints','examples')}|
        {'language':'python3','ioMode':'standardInputOutput'},id_factory=lambda:'probe-problem')
    problems.create(package)
    fields=[{'featureKey':category+'.primary','category':category,'status':'unknown',
        'reason':'本实验直接使用人工给定的题面，不额外推断结构字段。','evidenceRefs':[]}
        for category in ('dataScale','inputStructure','outputStructure','resourceConstraint','domainTag')]
    features=build_human_feature_set(package,{'expectedDraftRevision':1,'features':fields})
    problems.save_feature_set(features)
    problems.freeze(package['problemId'],1,features['featureSetId'],1)
    service=CoachService(path)
    workspace=service.editor.workspace(package['problemId'],1,create=True);goal=workspace['goal']['goalId']
    service.editor.save_draft(goal,{'sourceCode':case['code'],'expectedRevision':0,'operationId':'seed'})
    session=service.workspace(goal,create=True)['session']['sessionId']
    return service,goal,session


class TimedProvider:
    def __init__(self,provider,log=None):self.provider=provider;self.public_config=provider.public_config;self.calls=[];self.log=log
    def complete(self,**kwargs):
        start=time.monotonic()
        try:
            result=self.provider.complete(**kwargs)
            self.calls.append({'seconds':round(time.monotonic()-start,3),'inputChars':len(json.dumps(kwargs['messages'],ensure_ascii=False)),
                'outputChars':len(json.dumps(result,ensure_ascii=False)),'response':result})
            if self.log:self.log.write_text(json.dumps(self.calls,ensure_ascii=False,indent=2),encoding='utf-8')
            return result
        except Exception as exc:
            self.calls.append({'seconds':round(time.monotonic()-start,3),'failure':getattr(exc,'code',type(exc).__name__),
                'responseDiagnostic':getattr(exc,'response_diagnostic',None),
                'responseAudit':getattr(exc,'response_audit',None) or getattr(self.provider,'last_response_audit',{})})
            if self.log:self.log.write_text(json.dumps(self.calls,ensure_ascii=False,indent=2),encoding='utf-8')
            raise


SMOKE={'statement':'输出所给整数序列的总和。','inputSpec':'一行由空格分隔的整数。','outputSpec':'输出一个整数，总和。',
    'constraints':'1到100个整数，每个整数绝对值不超过1000。','examples':[{'input':'1 2 3\n','output':'6\n'}],
    'code':'a=list(map(int,input().split()))\ns=0\nfor i in range(len(a)-1):\n    s+=a[i]\nprint(s)\n',
    'ask':'为什么最后一个数没加进去？请帮我定位，不要给完整代码。'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--cycle',action='store_true')
    args=parser.parse_args();load_local_env(args.env_file)
    provider=TimedProvider(DeepSeekCoachProvider(replace(DeepSeekConfig.from_environment(),max_tokens=8192)))
    case=dict(SMOKE)
    if args.cycle:case['ask']='帮我修复漏加最后一个数的问题，先给局部提示，改好后继续帮我检查是否解决。'
    service,goal,session=seed(args.root,case)
    provider.log=args.root/'calls.json'
    service.command(session,{'operationId':'ask','kind':'ask','codeVersion':1,'payload':{'text':case['ask']}})
    worker=CoachWorker(service,provider)
    for _ in range(16):
        if not worker.process_once():break
        state=service.read(session)
        print(json.dumps({'round':state['round'],'actions':len(state['items'])},ensure_ascii=False),flush=True)
    if args.cycle:
        from services.oj_worker.editor_runs import RunRepository,RunWorker
        code=case['code'].replace('range(len(a)-1)','range(len(a))')
        service.editor.save_draft(goal,{'sourceCode':code,'expectedRevision':1,'operationId':'fix'})
        service.synchronize(session)
        runs=RunRepository(service.path)
        run=runs.enqueue({'goalContext':{'goalId':goal,'scope':'goal'},'problemRef':{'problemId':'probe-problem','problemVersion':1},
            'codeVersion':2,'runMode':'publicExample','operationId':'user-run'})
        start=time.monotonic();RunWorker(runs,args.root/'docker').process_once()
        print(json.dumps({'dockerSeconds':time.monotonic()-start,'run':runs.get(run['runId'])},ensure_ascii=False),flush=True)
        for _ in range(16):
            if not worker.process_once():break
            print(json.dumps({'round':service.read(session)['round']},ensure_ascii=False),flush=True)
    result={'model':provider.public_config,'calls':provider.calls,'state':service.manager.read(session)}
    (args.root/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'result':str(args.root/'result.json')},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
