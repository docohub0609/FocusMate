"""HTTP projection for the editor; execution remains in independent workers."""
from contextlib import closing
from http import HTTPStatus
import json
import re
import sqlite3
from urllib.parse import urlsplit, parse_qs

from .editor_storage import EditorRepository
from .problem_intake import new_request_id
from .problem_storage import StorageFailure
from .submission_storage import SubmissionFailure
from services.oj_worker.editor_runs import RunRepository, RunFailure
from services.oj_worker.judge import OJResultRepository


def history(editor, goal_id, query):
    editor.submissions.get_goal(goal_id)
    try:
        if set(query) - {'cursor','limit'} or any(len(v) != 1 for v in query.values()): raise ValueError()
        limit = int(query.get('limit', ['20'])[0]); cursor = query.get('cursor', [None])[0]
        if not 1 <= limit <= 100: raise ValueError()
        boundary = json.loads(cursor) if cursor else None
        if boundary is not None and (not isinstance(boundary,list) or len(boundary)!=2 or not all(isinstance(v,str) for v in boundary)): raise ValueError()
    except (ValueError, TypeError):
        raise SubmissionFailure('INVALID_HISTORY_PAGE', '历史分页参数无效。', 422)
    sql = '''SELECT * FROM (
        SELECT created_at,run_id AS id,'run' AS kind FROM editor_runs WHERE goal_id=?
        UNION ALL SELECT created_at,submission_id AS id,'submission' AS kind FROM submissions WHERE goal_id=?
        )'''
    params = [goal_id,goal_id]
    if boundary:
        sql += ' WHERE (created_at,id)<(?,?)'; params.extend(boundary)
    sql += ' ORDER BY created_at DESC,id DESC LIMIT ?'; params.append(limit+1)
    with closing(editor.connect()) as db: rows = db.execute(sql, params).fetchall()
    runs, results = RunRepository(editor.path), OJResultRepository(editor.path)
    items = []
    for row in rows[:limit]:
        if row['kind'] == 'run':
            task = runs.get(row['id']); task.pop('result',None)
        else:
            task = editor.submissions.get_submission(row['id'])
            result = results.get_for_submission(row['id'])
            task['verdict'] = result['verdict'] if result else None
        items.append(dict(task, kind=row['kind'], id=row['id']))
    next_cursor = json.dumps([rows[limit-1]['created_at'],rows[limit-1]['id']]) if len(rows)>limit else None
    return {'items':items,'nextCursor':next_cursor}


def handle_editor(handler, method):
    parsed = urlsplit(handler.path); path = parsed.path
    workspace = re.fullmatch(r'/api/v1/problems/([^/]+)/versions/(\d+)/editor-workspace', path)
    draft = re.fullmatch(r'/api/v1/goals/([^/]+)/code-draft', path)
    run = re.fullmatch(r'/api/v1/runs/([^/]+)', path)
    result = re.fullmatch(r'/api/v1/submissions/([^/]+)/result', path)
    executions = re.fullmatch(r'/api/v1/goals/([^/]+)/executions', path)
    if not (workspace or draft or run or result or executions or path == '/api/v1/runs'): return False
    request_id = new_request_id()
    payload = None
    if method in ('POST','PUT'):
        payload = handler._request_json(request_id)
        if not isinstance(payload, (dict,list,str,int,float,bool,type(None))): return True
    try:
        editor = EditorRepository(handler.problem_repository.path)
        runs = RunRepository(editor.path)
        status = HTTPStatus.OK
        if workspace and method in ('GET','POST'):
            if method == 'POST' and payload != {}: raise SubmissionFailure('INVALID_EDITOR_REQUEST','请使用空对象创建工作区。',422)
            response = editor.workspace(workspace[1], int(workspace[2]), create=method=='POST')
        elif draft and method == 'GET': response = editor.draft(draft[1])
        elif draft and method == 'PUT': response = editor.save_draft(draft[1], payload)
        elif path == '/api/v1/runs' and method == 'POST':
            response = runs.enqueue(payload); status = HTTPStatus.ACCEPTED
        elif run and method == 'GET': response = runs.get(run[1])
        elif result and method == 'GET':
            submission = editor.submissions.get_submission(result[1])
            response = {'submission': submission, 'result': OJResultRepository(editor.path).get_for_submission(result[1])}
            from services.oj_worker.diagnostics import get_failure
            response['failure'] = get_failure(editor.path, result[1])
        elif executions and method == 'GET': response = history(editor, executions[1], parse_qs(parsed.query,keep_blank_values=True))
        else: raise SubmissionFailure('METHOD_NOT_ALLOWED','此接口不支持这个操作。',405)
        handler._send_json(status,response,request_id=request_id)
    except (SubmissionFailure,RunFailure,StorageFailure) as error:
        handler._send_json(HTTPStatus(error.status), {'requestId':request_id,'code':error.code,'message':error.message,'fieldErrors':[]},request_id=request_id)
    except sqlite3.Error:
        handler._send_json(HTTPStatus.SERVICE_UNAVAILABLE,{'requestId':request_id,'code':'EDITOR_STORAGE_UNAVAILABLE','message':'保存服务暂时不可用，请保留代码并重试。','fieldErrors':[]},request_id=request_id)
    return True
