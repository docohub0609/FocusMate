"""Isolated UI fixture: scripted model replies and FakeRunner, not AI quality evidence."""
import json
import os
import sys
import time
from pathlib import Path
from threading import Event, Thread
from apps.api.__main__ import create_server
from apps.api.problem_storage import ProblemRepository
from apps.api.coach_service import CoachService
from apps.api.submission_storage import SubmissionRepository
from tests.python.test_submission_storage import frozen_problem
from tests.python.test_dynamic_coach_loop import diagnosis, plan, review
from tests.python.test_coach_compact_flow import compact
from tests.python.test_runtime_subgoals import mutation
from services.oj_worker.editor_runs import RunRepository, RunWorker
from tests.python.test_oj_judge import run_result
from focusmate.core.dynamic_coach_roles import verification_bundle

if os.environ.get('T082_READBACK_DB'):
    # Read back an explicitly selected isolated real-model probe; no model or execution replay.
    path = Path(os.environ['T082_READBACK_DB'])
    server = create_server(port=0, problem_repository=ProblemRepository(path))
    print(json.dumps({'port': server.server_port}), flush=True)
    try: server.serve_forever()
    finally: server.server_close()
    raise SystemExit(0)

path = Path(os.environ['FOCUSMATE_DB_PATH'])
frozen_problem(path, 'ready')
SubmissionRepository(path)
service = CoachService(path)
stop = Event()


class Runner:
    def run(self, request): return run_result(b'1 2\n')


runs = RunWorker(RunRepository(path), path.parent / 'runs', runner=Runner())


def advance(sid):
    state = service.synchronize(sid)
    r = state['rounds'].get(state['currentRoundId'])
    if not r or state['status'] == 'paused' or r['status'] not in ('queued', 'processing'): return
    message = state['messages'][-1]['text']
    if '等待' in message: return
    step = service.manager.claim(sid, 'browser-scripted-worker', materials=service.materials(sid))
    if not step: return
    state = service.manager.read(sid); r = state['rounds'][step['roundId']]
    targets = [x['subgoalId'] for x in r['targets']]
    if step['kind'] == 'diagnosis':
        if '执行许可' in message:
            result = {'kind': 'toolRequest', 'call': {'callId': 'browser-execute', 'toolName': 'run_public_samples', 'arguments': {'codeVersion': state['codeVersion']}}}
        else: result = {'kind': 'diagnosis', 'value': diagnosis(), 'candidateGoals': []}
    elif step['kind'] == 'plan':
        if '执行许可' in message:
            result = {'kind': 'toolRequest', 'call': {'callId': 'browser-execute', 'toolName': 'run_public_samples', 'arguments': {'codeVersion': state['codeVersion']}}}
            service.complete(sid, step['stepId'], step['token'], result)
            return
        if '模拟建议失败' in message and not r.get('failureStepId'):
            service.complete(sid, step['stepId'], step['token'], {'kind':'failure','code':'PROVIDER_UNAVAILABLE'})
            return
        changes = []
        if '建立目标' in message and r['trigger'] == 'user' and not r['checkBatchDone']:
            change = mutation(); change['objective'] = '检查输出语句'; change['checks'] = ['输出语句符合本次检查要求']
            changes = [change]; targets = [change['key']]
        candidate = compact(targets=targets)
        if changes:candidate['goalChanges']=[{k:v for k,v in change.items() if k!='expectedVersion'} for change in changes]
        code = service.editor.submissions.get_code_snapshot(state['goalContext']['goalId'], state['codeVersion'])['sourceCode'] if state['codeVersion'] else None
        candidate['help']['content'] = '这是浏览器模拟建议，用来验证页面与版本关联。' + ('\n```python\n' + code + '\n```' if code else '')
        if r.get('reportRefs') and '# check-fail' in (code or '') and not r.get('failureStepId'):
            service.complete(sid, step['stepId'], step['token'], {'kind':'failure','code':'PROVIDER_UNAVAILABLE'})
            return
        if targets:candidate['help']['followUp']={'kind':'awaitEvidence','targets':targets,'checks':['codeStructure'],
            'reason':'修改后检查此处','expectedUserAction':'改好输出语句后，检查这一步。'}
        if r.get('workflowVersion',1)>=3:
            candidate['targets']=[{'key':key,'followUp':True} for key in targets]
            if targets:candidate['help']['followUp'].pop('targets')
        result = {'kind': 'plan', 'value': candidate}
    elif step['kind'] == 'review': result = review()
    elif step['kind'] == 'verification':
        bundle = verification_bundle(state, r, step)
        code = service.editor.submissions.get_code_snapshot(state['goalContext']['goalId'], state['codeVersion'])['sourceCode']
        result = {'kind': 'verification', 'value': {'checks': [{'checkId': c['checkId'], **({'conditionStatus':'valid'} if r.get('workflowVersion',1)>=3 else {}), 'status': 'failed' if '# check-fail' in code else 'passed',
            'reason': 'UNREVIEWED FULL ANSWER', 'quotes': [{'ref': 'code:' + str(state['codeVersion']), 'text': code}],
            'evidenceType': 'codeStructureCheck', 'limitations': ['UNREVIEWED LIMITATION']} for c in bundle['checks']]}}
    else: result = {'kind': 'failure', 'code': 'PROVIDER_UNAVAILABLE'}
    service.complete(sid, step['stepId'], step['token'], result)


def work():
    follow_up_after = 0
    while not stop.wait(.15):
        try:
            if runs.process_once(): follow_up_after = time.monotonic() + 4
            if time.monotonic() >= follow_up_after: service.process_events()
            for sid in service.sessions(): advance(sid)
        except Exception as exc:
            print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr, flush=True)


Thread(target=work, daemon=True).start()
server = create_server(port=0, problem_repository=ProblemRepository(path))
print(json.dumps({'port': server.server_port}), flush=True)
try: server.serve_forever()
finally: stop.set(); server.server_close()
