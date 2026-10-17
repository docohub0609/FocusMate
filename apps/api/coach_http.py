"""Fast Coach registration and durable queries; no model calls here."""
from http import HTTPStatus
import re
import sqlite3
from urllib.parse import urlsplit,parse_qs
from .coach_service import CoachService
from .problem_intake import new_request_id
from .problem_storage import StorageFailure
from .submission_storage import SubmissionFailure
from focusmate.contracts.manager import ManagerError


def handle_coach(handler,method):
    parsed=urlsplit(handler.path)
    workspace=re.fullmatch(r'/api/v1/goals/([^/]+)/coach-workspace',parsed.path)
    completion=re.fullmatch(r'/api/v1/goals/([^/]+)/completion',parsed.path)
    work_goal=re.fullmatch(r'/api/v1/coach-sessions/([^/]+)/work-goals/([^/]+)',parsed.path)
    session=re.fullmatch(r'/api/v1/coach-sessions/([^/]+)(?:/(commands|checks/([^/]+)))?',parsed.path)
    if not (workspace or session or completion or work_goal):return False
    request_id=new_request_id()
    payload=handler._request_json(request_id) if method=='POST' else None
    if method=='POST' and not isinstance(payload,(dict,list,str,int,float,bool,type(None))):return True
    try:
        service=CoachService(handler.problem_repository.path);status=HTTPStatus.OK
        if completion and method=='GET':response=service.completion(completion[1])
        elif work_goal and method=='GET':
            query=parse_qs(parsed.query,keep_blank_values=True)
            if set(query)-{'version','limit','cursor'} or any(len(v)!=1 for v in query.values()):raise ManagerError('INVALID_HISTORY_PAGE')
            try:
                version=int(query['version'][0]) if 'version' in query else None
                limit=int(query.get('limit',['20'])[0]);cursor=int(query.get('cursor',['0'])[0])
            except ValueError:raise ManagerError('INVALID_HISTORY_PAGE') from None
            response=service.work_goal(work_goal[1],work_goal[2],version=version,limit=limit,cursor=cursor)
        elif workspace and method in ('GET','POST'):
            if method=='POST' and payload!={}:raise ManagerError('INVALID_COACH_REQUEST')
            response=service.workspace(workspace[1],create=method=='POST')
        elif session and not session[2] and method=='GET':
            query=parse_qs(parsed.query,keep_blank_values=True)
            if set(query)-{'limit','cursor','goalCursor'} or any(len(v)!=1 for v in query.values()):raise ManagerError('INVALID_HISTORY_PAGE')
            try:limit=int(query.get('limit',['20'])[0]);cursor=int(query.get('cursor',['0'])[0])
            except ValueError:raise ManagerError('INVALID_HISTORY_PAGE') from None
            response=service.read(session[1],limit=limit,cursor=cursor,goal_cursor=query.get('goalCursor',[None])[0])
        elif session and session[2]=='commands' and method=='POST':
            response=service.command(session[1],payload);status=HTTPStatus.ACCEPTED
        elif session and session[3] and method=='GET':response=service.check_batch(session[1],session[3])
        else:raise SubmissionFailure('METHOD_NOT_ALLOWED','此接口不支持这个操作。',405)
        handler._send_json(status,response,request_id=request_id)
    except (StorageFailure,SubmissionFailure) as exc:
        handler._send_json(HTTPStatus(exc.status),{'code':exc.code,'message':exc.message,'requestId':request_id},request_id=request_id)
    except ManagerError as exc:
        code=str(exc)
        status=404 if code.endswith('NOT_FOUND') or code=='UNKNOWN_SESSION' else 409 if any(x in code for x in ('CONFLICT','CHANGED','MISMATCH','STALE')) else 422
        handler._send_json(HTTPStatus(status),{'code':code,'message':'请求未被接受，请根据当前会话状态核对。','requestId':request_id},request_id=request_id)
    except sqlite3.Error:
        handler._send_json(HTTPStatus.SERVICE_UNAVAILABLE,{'code':'COACH_STORAGE_UNAVAILABLE','message':'辅导记录暂时不可用，请保留本次操作编号。','requestId':request_id},request_id=request_id)
    return True
