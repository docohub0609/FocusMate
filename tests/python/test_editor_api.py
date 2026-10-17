from tests.python.test_submission_api import running_api, request


def test_editor_http_flow_and_result_states(tmp_path):
    with running_api(tmp_path) as base:
        path = '/api/v1/problems/problem-submit/versions/1/editor-workspace'
        status, workspace = request(base, 'POST', path, {})
        assert status == 200
        goal = workspace['goal']['goalId']
        status, draft = request(base, 'PUT', f'/api/v1/goals/{goal}/code-draft',
            dict(sourceCode='print(input())', expectedRevision=0, operationId='save'))
        assert status == 200 and draft['codeVersion'] == 1
        payload = dict(goalContext=draft['goalContext'], problemRef=workspace['goal']['problemRef'], codeVersion=1,
            runMode='customInput', stdin='hello', operationId='run')
        status, run = request(base, 'POST', '/api/v1/runs', payload)
        assert status == 202
        assert request(base, 'GET', '/api/v1/runs/'+run['runId'])[1]['status'] == 'queued'
        status, submission = request(base, 'POST', '/api/v1/submissions',
            dict(goalContext=draft['goalContext'], problemRef=workspace['goal']['problemRef'], codeVersion=1,
                 language='python3', idempotencyKey='submit'))
        assert status == 201
        status, result = request(base, 'GET', '/api/v1/submissions/'+submission['submissionId']+'/result')
        assert status == 200 and result['result'] is None
        status, history = request(base, 'GET', f'/api/v1/goals/{goal}/executions')
        assert status == 200 and len(history['items']) == 2
        assert {i['kind'] for i in history['items']} == {'run','submission'}
        assert request(base,'GET','/api/v1/submissions/missing/result')[0] == 404


def test_history_default_page_and_cursor_keep_each_record_once(tmp_path):
    from apps.api.editor_http import history
    from apps.api.editor_storage import EditorRepository
    from tests.python.test_editor_runs import setup_run
    runs, payload = setup_run(tmp_path)
    expected = {runs.enqueue(dict(payload,operationId=str(i)))['runId'] for i in range(23)}
    editor = EditorRepository(runs.path)
    first = history(editor,payload['goalContext']['goalId'],{})
    assert len(first['items']) == 20 and first['nextCursor']
    second = history(editor,payload['goalContext']['goalId'],{'cursor':[first['nextCursor']]})
    assert len(second['items']) == 3 and second['nextCursor'] is None
    assert {item['id'] for item in first['items']+second['items']} == expected
