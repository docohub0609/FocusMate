"""Real T071 flows; developer expectations are saved separately, never prompted."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src'),str(ROOT/'tools')]
from diagnosis_cases import cases,material_bundle,SUM
from diagnosis_runtime import run_diagnosis,enabled_skills
from apps.api.deepseek_provider import load_local_env
from apps.api.problem_intake import create_problem_package
from apps.api.problem_storage import ProblemRepository
from apps.api.problem_features import build_human_feature_set
from apps.api.submission_storage import SubmissionRepository
from focusmate.adapters.tool_chat import DeepSeekToolChatProvider
from focusmate.adapters.tool_storage import SQLiteToolBackend
from focusmate.adapters.tool_ledger import SQLiteToolLedger
from focusmate.adapters.manager_runs import ManagerRunLookup
from focusmate.core.tool_gateway import ToolGateway
from focusmate.core.context_builder import MapContextResolver,hash_content
from focusmate.core.diagnosis import prepare_diagnosis,PROMPT_VERSION
from services.oj_worker.tool_public_runs import PublicRunService


def save(path,value):
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)


def seed(path,case):
    intake={'statement':case['problem'],'inputSpec':'第一行n target，第二行n个整数。' if case['problem']==SUM else '使用题目所述输入。',
            'outputSpec':'输出两个下标。' if case['problem']==SUM else '按题目要求输出。',
            'constraints':'按题面明确的条件。','examples':[{'input':'4 9\n2 7 11 15\n','output':'0 1\n'}, {'input':'2 6\n3 3\n','output':'0 1\n'}] if case['problem']==SUM else [],
            'language':'python3','ioMode':'standardInputOutput'}
    # Non-execution scenarios need only a stored snapshot; no invented run evidence.
    if not intake['examples']:intake['examples']=[{'input':'1\n','output':'1\n'}]
    package=create_problem_package(intake,id_factory=lambda:'problem-'+case['id'])
    repo=ProblemRepository(path);record=repo.create(package)
    features=[{'featureKey':'input.primary','category':'inputStructure','status':'confirmed','value':intake['inputSpec'],
               'confidence':1,'reason':'开发材料显式规定','evidenceRefs':['inputSpec:0']}]
    for key,category,field in [('scale.primary','dataScale','constraints'),('output.primary','outputStructure','outputSpec')]:
        features.append({'featureKey':key,'category':category,'status':'confirmed','value':intake[field],
                         'confidence':1,'reason':'开发材料直接引用','evidenceRefs':[field+':0']})
    for key,category in [('resource.primary','resourceConstraint'),('domain.primary','domainTag')]:
        features.append({'featureKey':key,'category':category,'status':'unknown','reason':'本实验不据此推断','evidenceRefs':[]})
    report=build_human_feature_set(package,{'expectedDraftRevision':1,'features':features})
    repo.confirm_and_freeze(package['problemId'],1,report,record['lifecycle']['draftRevision'],None)
    submissions=SubmissionRepository(path);goal=submissions.create_goal(report['problemRef'])
    if case.get('oldRun'):submissions.create_code_snapshot(goal['goalId'],'print(0)\n','python3')
    if case['code'] is not None:submissions.create_code_snapshot(goal['goalId'],case['code'],'python3')
    if case.get('tools')==['run_public_samples']:
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE public_test_cases(test_case_id TEXT,asset_id TEXT,asset_version INTEGER,problem_id TEXT,problem_version INTEGER,example_index INTEGER,stdin_text TEXT,expected_output_text TEXT)')
            for i,e in enumerate(intake['examples']):db.execute('INSERT INTO public_test_cases VALUES(?,?,?,?,?,?,?,?)',(f'sample-{i}','public',1,package['problemId'],1,i,e['input'],e['output']))
    return goal


class FaultBackend(SQLiteToolBackend):
    def load_submission(self,*args):raise RuntimeError('explicit simulated lookup outage')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--group',choices=['development','holdout'],required=True)
    p.add_argument('--repeats',type=int,default=1);p.add_argument('--env-file',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--case',action='append')
    a=p.parse_args();a.output_dir.mkdir(parents=True,exist_ok=False);load_local_env(a.env_file)
    selected=[c for c in cases(a.group) if not a.case or c['id'] in a.case]
    config=json.loads((ROOT/'skills/diagnosis-enabled.json').read_text(encoding='utf-8'));skills=enabled_skills(config)
    provider=DeepSeekToolChatProvider.from_environment()
    save(a.output_dir/'manifest.json',{'group':a.group,'repeats':a.repeats,'promptVersion':PROMPT_VERSION,
         'skills':config,'modelConfig':provider.public_config,'cases':selected,'cost':'unknown',
         'reviewer':'main Codex developer, pending source-level content review','source':'developer-authored cases; actual model and SQLite/Docker tools; lookup outage separately simulated'})
    def run(pair):
        case,repeat=pair;directory=a.output_dir/(case['id']+'-'+str(repeat));directory.mkdir();start=time.monotonic()
        try:
            goal=seed(directory/'business.sqlite3',case);req,records=material_bundle(case,goal['goalId'],goal['problemRef']['problemId'])
            bundle=prepare_diagnosis(req,MapContextResolver(records));save(directory/'input.json',bundle)
            public=PublicRunService(directory/'business.sqlite3',directory/'public') if 'run_public_samples' in case.get('tools',[]) else None
            backend=(FaultBackend if case.get('fault') else SQLiteToolBackend)(directory/'business.sqlite3',public)
            gateway=ToolGateway(backend,SQLiteToolLedger(directory/'tool-calls.sqlite3'));gateway.diagnosis_allowed_tools=case.get('tools',[])
            state=run_diagnosis(bundle,provider,directory,skills=skills,gateway=gateway,backend=backend,
                                lookup=ManagerRunLookup(public,None) if public else None)
            save(directory/'trace.json',state);r=state['rounds'][state['currentRoundId']]
            save(directory/'result.json',r)
            result={'case':case['id'],'repeat':repeat,'status':r['status'],'failure':r['failure'],'counts':r['counts'],
                    'seconds':round(time.monotonic()-start,3),'contentReview':'pending'}
        except Exception as exc:
            result={'case':case['id'],'repeat':repeat,'status':'setupFailed','failure':str(exc),'seconds':time.monotonic()-start}
        save(directory/'metrics.json',result);print(json.dumps(result,ensure_ascii=False),flush=True);return result
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[(c,i) for c in selected for i in range(1,a.repeats+1)]))
    save(a.output_dir/'summary.json',results)


if __name__=='__main__':main()
