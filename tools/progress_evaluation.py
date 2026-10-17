"""Fixed developer scenarios; expectations stay outside the model request."""
from copy import deepcopy
from datetime import datetime, timezone
from difflib import unified_diff
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256

from focusmate.core.context_builder import hash_content, MapContextResolver
from focusmate.core.goal_template import build_generic_algorithm_goal_contract
from focusmate.core.progress_estimation import estimate_progress, PROGRESS_PROMPT, PROGRESS_PROMPT_VERSION

ROOT=Path(__file__).resolve().parents[1]
FIXTURE=ROOT/'tests/fixtures/t063/progress-cases.json'


def expand(spec):
    context={'goalId':'goal-01','scope':'goal'};problem={'problemId':'problem-01','problemVersion':1}
    goal={'goalId':context['goalId'],'schemaVersion':'1.0.0','createdAt':'2026-09-19T00:00:00Z','problemRef':problem,'status':'active'}
    contract=build_generic_algorithm_goal_contract(goal,created_at=goal['createdAt'])
    contract.update(objective=spec['goal'],contractStatus='frozen')
    contract['subgoals']=[{'subgoalId':'s1','stage':'coreImplementation','objective':spec['goal'],'dependsOn':[],
        'acceptableEvidence':['codeStructureCheck','evaluatedUserExplanation','publicSampleRunResult','targetedRunResult']}]
    records=[]
    def add(ref,kind,content,version=1,**extra):
        records.append({'ref':ref,'kind':kind,'version':version,'goalContext':context,'content':content,
            'contentHash':hash_content(content),'origin':'developer-fixture',**extra})
    add('g01','goal',contract)
    if spec.get('before') is not None:add('c01','code',{'snapshotRef':'c01','language':'python3','text':spec['before']})
    add('c02','code',{'snapshotRef':'c02','language':'python3','text':spec['after']},2)
    if spec.get('user'):add('u01','dialogue',{'ref':'u01','role':'user','text':spec['user'],'codeSnapshotRef':'c02'},sequence=1,turnId='turn-01')
    for index,run in enumerate(spec.get('runs',[]),1):add(f'r{index:02}','runEvidence',deepcopy(run))
    observation={'observationId':'obs-01','schemaVersion':'2.0.0','createdAt':'2026-09-19T00:00:00Z',
        'goalContext':context,'sourceEventIds':['event-01'],'kind':'codeChange','trigger':'developerFixture',
        'priority':'observationReady','evidence':{'startVersion':1 if spec.get('before') is not None else None,'endVersion':2,
            'codeRefs':['c01','c02'] if spec.get('before') is not None else ['c02'],
            'firstReceivedAt':goal['createdAt'],'lastReceivedAt':goal['createdAt'],
            'eventTimes':[{'eventId':'event-01','createdAt':goal['createdAt']}]},'uncertainty':[],
        'delta':{'changed':spec.get('before')!=spec['after'] if spec.get('before') is not None else None,
            'diff':''.join(unified_diff(spec['before'].splitlines(True),spec['after'].splitlines(True))) if spec.get('before') is not None else '', 'truncated':False},
        'organization':{'scope':{'goalContext':context,'problemRef':problem,'contractVersion':1,'sessionRef':None},
            'policy':{'quiet_seconds':2,'max_seconds':30,'diff_max_bytes':8192,'rule_version':'t061-v1'}}}
    add('o01','observation',{'observation':observation})
    req={'schemaVersion':'1.0.0','goalContext':context,'problemRef':problem,'contractVersion':1,
         'contractRef':'g01','currentCodeRef':'c02','baselineRefs':['c01'] if spec.get('before') is not None else [],
         'sources':[{'ref':r['ref'],'kind':r['kind'],'version':r['version'],'contentHash':r['contentHash'],
            'sourceType':'external','sourceId':r['ref']} for r in records],
         'budget':{'contextWindowTokens':24000,'reservedOutputTokens':8192,'safetyMarginTokens':1000}}
    return {'id':spec['id'],'group':spec['group'],'request':req,'records':records,'expected':deepcopy(spec['expected']),
        'factChecks':deepcopy(spec.get('factChecks',[]))}


def load_cases(path=FIXTURE):return [expand(s) for s in json.loads(Path(path).read_text(encoding='utf-8'))]


def save(path,value):
    with Path(path).open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)


def run_evaluation(cases,provider,output,*,repeats=2,workers=1):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    config={k:v for k,v in provider.public_config.items() if k in ('provider','model','maxTokens','thinkingEnabled','reasoningEffort','timeoutSeconds','networkAttempts')}
    manifest={'createdAt':datetime.now(timezone.utc).isoformat(),'promptVersion':PROGRESS_PROMPT_VERSION,
        'prompt':PROGRESS_PROMPT,'modelConfig':config,'cases':deepcopy(cases),'repeats':repeats,
        'reviewer':'Pending primary Codex content review; not automatic scoring or independent blind review.',
        'source':'Developer-authored scenarios; factChecks identify actual executed calculations or simulated conflicts.'}
    paths=['src/focusmate/core/progress_estimation.py','src/focusmate/core/progress_materials.py',
        'src/focusmate/adapters/progress_estimation.py','tools/progress_evaluation.py',
        'schemas/progress/progress-estimation-candidates.schema.json','tests/fixtures/t063/progress-cases.json']
    manifest['implementationHashes']={p:sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}
    manifest['contentHash']=hash_content(manifest);save(output/'manifest.json',manifest)
    def execute(case,repeat):
        report=estimate_progress(case['request'],MapContextResolver(case['records']),provider)
        item={'caseId':case['id'],'repeat':repeat,'report':report}
        save(output/f"response-{case['id']}-{repeat}.json",item)
        return {'caseId':case['id'],'repeat':repeat,'status':report['status'],'failure':report.get('failure')}
    results=[]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures=[pool.submit(execute,case,r) for case in cases for r in range(1,repeats+1)]
        for future in as_completed(futures):
            result=future.result();results.append(result);print(json.dumps(result,ensure_ascii=False),flush=True)
    save(output/'execution-summary.json',{'results':results,'contentAcceptance':'not yet reviewed','cost':'unknown'})
    return results
