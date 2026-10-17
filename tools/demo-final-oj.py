"""Isolated developer fixture + real Docker/OJ, not a live user or model evaluation."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT),str(ROOT/'src')]
from apps.api.problem_intake import create_problem_package
from apps.api.problem_features import build_human_feature_set
from apps.api.problem_storage import ProblemRepository
from apps.api.submission_storage import SubmissionRepository
from apps.api.trusted_assets import TrustedAssetBuilder
from apps.api.verifier_assets import VerifierAssetRepository
from apps.api.goal_contract_storage import _TABLES
from focusmate.adapters.final_oj import SQLiteFinalOJStore
from focusmate.adapters.manager_storage import SQLiteManagerStore
from focusmate.core.final_oj import GoalStateManager
from focusmate.core.goal_template import build_generic_algorithm_goal_contract
from focusmate.core.session_manager import SessionManager
from services.oj_worker.docker_runner import DockerPythonRunner
from services.oj_worker.judge import JudgeSubmissionProcessor, OJResultRepository
from services.oj_worker.queue import WorkerQueue
from services.oj_worker.sqlite_assets import SQLiteTrustedAssetProvider
from services.oj_worker.worker import Worker
import sqlite3


def run(output):
    output.mkdir(parents=True,exist_ok=False)
    path = output/'business.sqlite3'
    now = datetime.now(timezone.utc).isoformat()
    problem = {'statement':'Read an integer array and output the sum of all its elements.',
        'inputSpec':'The first line is n. The second line contains n integers.',
        'outputSpec':'Output one integer: the sum.', 'constraints':'1 <= n <= 100000',
        'examples':[{'input':'3\n1 2 3\n','output':'6\n'}],
        'language':'python3','ioMode':'standardInputOutput'}
    problems = ProblemRepository(path)
    record = problems.create(create_problem_package(problem,id_factory=lambda:'sum-array'))
    features = [
        {'featureKey':'dataScale.n','category':'dataScale','status':'confirmed',
         'value':{'variable':'n','minimum':1,'maximum':100000},'reason':'Explicit n bounds.','evidenceRefs':['constraints:0']},
        {'featureKey':'input.primary','category':'inputStructure','status':'confirmed',
         'value':{'kind':'integerSequence','lengthVariable':'n'},'reason':'n integers.','evidenceRefs':['inputSpec:0']},
        {'featureKey':'output.primary','category':'outputStructure','status':'confirmed',
         'value':{'valueCount':1,'valueType':'integer','meanings':['sum']},'reason':'One sum.','evidenceRefs':['outputSpec:0']},
        {'featureKey':'resource.primary','category':'resourceConstraint','status':'unknown','reason':'No resource limit supplied.','evidenceRefs':[]},
        {'featureKey':'domain.array','category':'domainTag','status':'confirmed','value':'array','reason':'Integer array.','evidenceRefs':['statement:0']}]
    for feature in features:
        if feature['status'] == 'confirmed': feature['confidence'] = 1
    feature_set = build_human_feature_set(record['problemPackage'],{'expectedDraftRevision':1,'features':features})
    problems.confirm_and_freeze('sum-array',1,feature_set,1,None)
    private = [('negative','1\n-7\n','-7\n'),('zero','4\n-3 0 1 2\n','0\n'),('repeated','4\n2 2 2 2\n','8\n')]
    # Fixed known answers are independently recomputed while preparing the fixture.
    for _,text,answer in private:
        assert sum(map(int,text.split()[1:])) == int(answer)
    material = TrustedAssetBuilder(path).build({'manifestId':'developer-sum-fixture','schemaVersion':'1.0.0',
        'problemRef':{'problemId':'sum-array','problemVersion':1},
        'privateTests':[{'caseKey':k,'stdin':text,'answerSource':'expectedOutput','expectedOutput':answer} for k,text,answer in private]})
    VerifierAssetRepository(path).complete_cross_check('sum-array',1,{
        'reportId':'developer-fixed-arithmetic-check','outcome':'passed','checkedMaterialHash':material['materialHash'],
        'checksRun':['referenceAgreement','privateCoveragePresent'],'createdAt':now})
    repo = SubmissionRepository(path)
    goal = repo.create_goal({'problemId':'sum-array','problemVersion':1})
    contract = build_generic_algorithm_goal_contract(goal,created_at=now); contract['contractStatus']='frozen'
    from focusmate.contracts.final_oj import digest
    with sqlite3.connect(path) as db:
        for sql in _TABLES: db.execute(sql)
        db.execute('INSERT INTO goal_contract_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (goal['goalId'],1,1,'frozen',digest(contract),json.dumps({'contract':contract}),
             '{}',None,'developer-frozen template; no model contract review',now,now,now,None))
    oj = OJResultRepository(path)
    executions = []
    class RecordedRunner:
        def run(self, req):
            result = DockerPythonRunner().run(req)
            executions.append({'operation':req.operation,'terminationReason':result.termination_reason,
                'exitCode':result.exit_code,'wallTimeMs':result.resource_summary.wall_time_ms})
            return result
    worker = Worker(WorkerQueue(path),JudgeSubmissionProcessor(RecordedRunner(),SQLiteTrustedAssetProvider(path),oj),
                    't074-real-docker',output/'workspaces')
    final = GoalStateManager(SQLiteFinalOJStore(path))
    sessions = SessionManager(SQLiteManagerStore(output/'manager.sqlite3'))
    reports = []
    for version,source in enumerate([
        'n = int(input())\nvalues = list(map(int, input().split()))\nprint(sum(values) + 1)\n',
        'n = int(input())\nvalues = list(map(int, input().split()))\nprint(sum(values))\n'],start=1):
        code = repo.create_code_snapshot(goal['goalId'],source,'python3')
        sub,_ = repo.create_submission({'goalContext':{'goalId':goal['goalId'],'scope':'goal'},
            'problemRef':goal['problemRef'],'codeVersion':version,'language':'python3','idempotencyKey':f'submit-{version}'})
        req = {'schemaVersion':'1.0.0','requestId':f'final-{version}','goalId':goal['goalId'],
               'goalContractVersion':1,'submissionId':sub['submissionId']}
        session = f'demo-{version}'
        sessions.create(session,{'goalId':goal['goalId'],'scope':'goal'},goal['problemRef'],
                        contract_version=1,code_version=version,allowed_tools=[])
        receipt = sessions.start_final_verification(session,req,final)
        before = sessions.advance_final_verification(session,final)
        execution_start = len(executions)
        started = time.monotonic(); worker_status = worker.process_once()
        after = sessions.advance_final_verification(session,final)
        result = oj.get_for_submission(sub['submissionId'])
        item = {'codeVersion':version,'sourceCode':source,'before':before,'workerStatus':worker_status,
                'ojVerdict':result['verdict'] if result else None,'after':after,
                'goalState':final.goal_state(goal['goalId']),'durationSeconds':time.monotonic()-started,
                'managerReceipt':receipt,'executions':executions[execution_start:]}
        reports.append(item)
        (output/f'run-{version}.json').write_text(json.dumps(item,ensure_ascii=False,indent=2),encoding='utf-8')
    successful = reports[0]['ojVerdict'] == 'WA' and reports[0]['after'].get('verifierResult',{}).get('status') == 'failed' and reports[1]['goalState']['status'] == 'completed'
    summary = {'origin':'developer-prepared complete problem, frozen generic contract and arithmetic-checked test assets',
        'execution':'real Docker, production submission/worker/result/event/final-state storage',
        'contractReview':'not exercised; developer fixture explicitly seeded', 'modelCalls':0,
        'status':'passed' if successful else 'incomplete','verdicts':[r['ojVerdict'] for r in reports],
        'goalStates':[r['goalState']['status'] for r in reports]}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False))
    return 0 if successful else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--output-dir',type=Path,required=True)
    raise SystemExit(run(p.parse_args().output_dir))
