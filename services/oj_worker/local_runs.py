"""Persist one bounded local-code experiment and execute its fixed template in Docker."""
from __future__ import annotations

import ast
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import textwrap
import tokenize
from io import StringIO
from uuid import uuid4

from .docker_runner import DockerPythonRunner, LOCAL_POLICY, PythonRunRequest
from .tool_public_runs import PublicRunService


TEMPLATE_VERSION = 'python-local-v1'
OBSERVATION_PREFIX = '__FOCUSMATE_OBSERVATION_V1__'
_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode()).hexdigest()


def _identifier(value):
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) and not value.startswith('__')


def _validate_json(value):
    encoded = _json(value)
    if len(encoded.encode()) > 4096:
        raise ValueError('JSON setup too large')
    return encoded


def _source_segment(source, node):
    lines = source.splitlines()
    start = min([node.lineno, *[d.lineno for d in getattr(node, 'decorator_list', [])]])
    end = node.end_lineno
    return start, end, '\n'.join(lines[start - 1:end])


def _parse(source):
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        raise ValueError('snippet is not complete syntax') from exc


def build_local_program(code, request):
    source = code['sourceCode']
    if sha256(source.encode()).hexdigest() != code['contentHash'].removeprefix('sha256:'):
        raise ValueError('source mismatch')
    if len(source.encode()) > 256 * 1024:
        raise ValueError('source too large')
    selector = request['snippet']
    if selector['kind'] == 'function':
        lines=source.splitlines();starts=[];whole=None
        try:whole=ast.parse(source)
        except SyntaxError:pass
        if whole is not None:
            matches=[node for node in whole.body if isinstance(node,ast.FunctionDef) and node.name==selector['name']]
            if len(matches)!=1 or matches[0].decorator_list:raise ValueError('unsupported function')
            start,end,selected_source=_source_segment(source,matches[0]);selected=[matches[0]]
        else:
            # A complete early function remains usable when unrelated later work is
            # syntactically unfinished. Tokenization avoids matching text in strings.
            string_lines=set();selected_decorated=False;decorator_pending=False
            try:
                tokens=tokenize.generate_tokens(StringIO(source).readline)
                for token in tokens:
                    if token.type==tokenize.STRING:
                        string_lines.update(range(token.start[0],token.end[0]+1))
                    if token.type==tokenize.OP and token.string=='@' and token.start[1]==0:
                        decorator_pending=True
                    if token.type==tokenize.NAME and token.string=='def' and token.start[1]==0:
                        name=next(tokens)
                        if name.type==tokenize.NAME and name.string==selector['name']:
                            starts.append(token.start[0]-1);selected_decorated=decorator_pending
                        decorator_pending=False
            except tokenize.TokenError:
                pass
            except IndentationError:
                pass
            if len(starts)!=1 or selected_decorated:
                raise ValueError('unsupported function')
            first=starts[0];last=len(lines)
            previous=first-1
            while previous>=0 and not lines[previous].strip():previous-=1
            if previous>=0 and lines[previous].lstrip().startswith('@'):
                raise ValueError('decorated function unsupported')
            for index in range(first+1,len(lines)):
                if (lines[index].strip() and not lines[index][0].isspace()
                        and not lines[index].lstrip().startswith('#') and index+1 not in string_lines):
                    last=index;break
            while last>first+1 and not lines[last-1].strip(): last-=1
            selected_source='\n'.join(lines[first:last])
            parsed=_parse(selected_source)
            if len(parsed.body)!=1 or not isinstance(parsed.body[0],ast.FunctionDef) or parsed.body[0].decorator_list:
                raise ValueError('unsupported function')
            selected=parsed.body;start=first+1;end=last
    else:
        start, end = selector['startLine'], selector['endLine']
        lines=source.splitlines()
        if start > end or end>len(lines):
            raise ValueError('invalid range')
        try:whole=ast.parse(source)
        except SyntaxError:whole=None
        if whole is not None:
            def original_start(node):
                return min([node.lineno,*[item.lineno for item in getattr(node,'decorator_list',[])]])
            exact=[node for node in ast.walk(whole) if isinstance(node,ast.stmt)
                   and original_start(node)>=start and node.end_lineno<=end]
            if (not exact or min(original_start(node) for node in exact)!=start
                    or max(node.end_lineno for node in exact)!=end):
                raise ValueError('range must match complete original statements')
        else:
            string_ranges=[];decorated_defs=set();decorator_pending=False
            try:
                tokens=tokenize.generate_tokens(StringIO(source).readline)
                for token in tokens:
                    if token.type==tokenize.STRING:string_ranges.append((token.start[0],token.end[0]))
                    if token.type==tokenize.OP and token.string=='@' and token.start[1]==0:
                        decorator_pending=True
                    if token.type==tokenize.NAME and token.string=='def' and token.start[1]==0:
                        if decorator_pending:decorated_defs.add(token.start[0])
                        decorator_pending=False
            except tokenize.TokenError as exc:
                if len(exc.args)>1 and isinstance(exc.args[1],tuple):
                    string_ranges.append((exc.args[1][0],len(lines)))
            except IndentationError:
                pass
            if any(a<=start<=b or a<=end<=b for a,b in string_ranges):
                raise ValueError('range is inside a string literal')
        original_source='\n'.join(lines[start-1:end])
        selected_source=textwrap.dedent(original_source)
        parsed=_parse(selected_source)
        selected=parsed.body
        if whole is None and any(isinstance(node,ast.FunctionDef) for node in selected):
            raise ValueError('function lines require a complete source snapshot or function selector')
        if original_source!=selected_source and any(isinstance(node,ast.Constant) and isinstance(node.value,(str,bytes))
                and getattr(node,'end_lineno',node.lineno)!=node.lineno for node in ast.walk(parsed)):
            raise ValueError('dedent would change multiline string')
        if not selected or min(n.lineno for n in selected)!=1 or max(n.end_lineno for n in selected)!=end-start+1:
            raise ValueError('range must contain complete top-level statements')
        if any(isinstance(n, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.AsyncFunctionDef))
               or isinstance(n,ast.FunctionDef) and n.decorator_list for n in selected):
            raise ValueError('unsupported dependency')
    if len(selected_source.encode()) > 8192:
        raise ValueError('snippet too large')
    setup = request.get('initialVariables', {})
    if not isinstance(setup, dict) or any(not _identifier(k) or k=='json' or k.startswith('_focusmate_') for k in setup):
        raise ValueError('invalid setup')
    setup_json = _validate_json(setup)
    call = request.get('call')
    if call is not None:
        if not _identifier(call.get('function')) or not isinstance(call.get('args'), list):
            raise ValueError('invalid call')
        _validate_json(call['args'])
        defined={n.name for n in selected if isinstance(n,ast.FunctionDef)}
        if call['function'] not in defined:
            raise ValueError('call must name an extracted function')
    observe = request.get('observe', {})
    identifiers = observe.get('identifiers', [])
    if (not isinstance(identifiers, list) or len(set(identifiers)) != len(identifiers)
            or any(not _identifier(x) for x in identifiers)):
        raise ValueError('invalid observations')
    if observe.get('returnValue') and call is None:
        raise ValueError('return value requires call')
    setup_literal = repr(setup_json)
    call_line = '_focusmate_return = None'
    if call:
        call_line = f"_focusmate_return = {call['function']}(*json.loads({repr(_json(call['args']))}))"
    observation_literal = repr(identifiers)
    program = ("import json\n"
               f"globals().update(json.loads({setup_literal}))\n"
               f"{selected_source}\n"
               f"{call_line}\n"
               f"_focusmate_names = {observation_literal}\n"
               "_focusmate_observed = {name: globals()[name] for name in _focusmate_names}\n"
               "_focusmate_payload = {'observations': _focusmate_observed}\n"
               + ("_focusmate_payload['returnValue'] = _focusmate_return\n" if observe.get('returnValue') else '')
               + f"print({repr(OBSERVATION_PREFIX)} + json.dumps(_focusmate_payload, ensure_ascii=False, allow_nan=False, sort_keys=True))\n")
    original='\n'.join(source.splitlines()[start-1:end])
    return {'program': program, 'sourceRef': {'kind': selector['kind'], 'startLine': start,
            'endLine': end, **({'name': selector['name']} if selector['kind'] == 'function' else {}),
            'source': original, 'executedSource': selected_source,
            'normalization':'none' if original==selected_source else 'textwrap.dedent'},
            'setup': setup, 'call': call, 'observe': observe}


class LocalRunService:
    def __init__(self, work_root, runner=None, task_database_path=None):
        self.root = Path(work_root).resolve(); self.root.mkdir(parents=True, exist_ok=True)
        self.path = Path(task_database_path) if task_database_path else self.root / 'tool-tasks.sqlite3'
        self.runner = runner
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tool_local_tasks(
                run_id TEXT PRIMARY KEY,dedup_key TEXT UNIQUE NOT NULL,status TEXT NOT NULL,
                owner TEXT,deadline REAL NOT NULL,loop_id TEXT NOT NULL,context_json TEXT NOT NULL,
                result_json TEXT,workspace TEXT NOT NULL,container_name TEXT)''')
            db.commit()

    def _row(self, run_id):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM tool_local_tasks WHERE run_id=?', (run_id,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _view(row):
        return json.loads(row['result_json']) if row['result_json'] else {'runId': row['run_id'], 'status': row['status'], **json.loads(row['context_json'])}

    def run(self, context, code, request):
        identity = {'goalContext': context.get('goalContext'), 'problemRef': context.get('problemRef'),
                    'codeVersion': code.get('codeVersion'), 'codeSnapshotId': code.get('codeSnapshotId')}
        try:
            deadline = min(datetime.fromisoformat(context['deadlineAt'].replace('Z', '+00:00')).timestamp(),
                           datetime.now(timezone.utc).timestamp() + 30)
            if deadline <= datetime.now(timezone.utc).timestamp() or request['codeVersion'] != code['codeVersion']:
                raise ValueError()
            built = build_local_program(code, request)
            exact = {k: request.get(k) for k in ('purpose','observationIntent','snippet','initialVariables','call','observe')}
            key = _digest([context['loopId'], identity, code['contentHash'], exact, TEMPLATE_VERSION])
        except (KeyError, ValueError, TypeError):
            return {'status': 'failed', 'failure': 'LOCAL_RUN_INPUT_INVALID', **identity}
        run_id='tool-local-'+uuid4().hex; owner=uuid4().hex; workspace=self.root/run_id
        stored_context={**identity, 'loopId':context['loopId'],'contentHash':code['contentHash'],
                        'request':exact,'sourceRef':built['sourceRef']}
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT run_id FROM tool_local_tasks WHERE dedup_key=?',(key,)).fetchone()
            if row: return self._view(self._row(row[0]))
            if db.execute('SELECT 1 FROM tool_local_tasks WHERE loop_id=?',(context['loopId'],)).fetchone():
                return {'status':'failed','failure':'LOCAL_RUN_LIMIT_REACHED',**identity}
            db.execute('INSERT INTO tool_local_tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (run_id,key,'running',owner,deadline,context['loopId'],_json(stored_context),None,str(workspace),None))
            db.commit()
        generated={'templateVersion':TEMPLATE_VERSION,'contentHash':sha256(built['program'].encode()).hexdigest(),
                   'source':built['program']}
        result={'runId':run_id,**identity,'purpose':request['purpose'],
                'observationIntent':request['observationIntent'],'sourceRef':built['sourceRef'],
                'setup':built['setup'],'call':built['call'],'observe':built['observe'],
                'generatedSourceRef':generated}
        try:
            workspace.mkdir(); (workspace/'main.py').write_text(built['program'],encoding='utf-8')
            remaining=deadline-datetime.now(timezone.utc).timestamp();row=self._row(run_id)
            if remaining<=0 or row['status']!='running' or row['owner']!=owner: raise InterruptedError()
            container_name='focusmate-local-'+uuid4().hex
            with closing(sqlite3.connect(self.path)) as db:
                changed=db.execute("UPDATE tool_local_tasks SET container_name=? WHERE run_id=? AND owner=? AND status='running'",
                                   (container_name,run_id,owner)).rowcount;db.commit()
            if not changed: raise InterruptedError()
            runner=self.runner or DockerPythonRunner(container_name_factory=lambda:container_name)
            run_request=PythonRunRequest(workspace,b'',policy=LOCAL_POLICY)
            run_request=replace(run_request,policy=replace(run_request.policy,
                                wall_time_seconds=min(run_request.policy.wall_time_seconds,remaining)))
            result['resourcePolicy']=asdict(run_request.policy)
            raw=runner.run(run_request)
            row=self._row(run_id)
            if row['status']!='running' or row['owner']!=owner or datetime.now(timezone.utc).timestamp()>=deadline:
                raise InterruptedError()
            stdout=raw.stdout.decode('utf-8',errors='replace')
            result['rawOutput']=stdout[:2048];result['rawOutputTruncated']=len(stdout)>2048
            errors=raw.stderr.decode('utf-8',errors='replace').splitlines()
            result['errorOutput']=next((line for line in reversed(errors)
                if re.match(r'^[A-Za-z]+(?:Error|Exception)(?::|$)',line)), '')[:512]
            result['terminationReason']=raw.termination_reason; result['exitCode']=raw.exit_code
            if raw.stdout_truncated or raw.stderr_truncated or raw.termination_reason != 'exited' or raw.exit_code in (137,-9):
                if raw.termination_reason in ('wallTimeExceeded','memoryLimitExceeded','outputLimitExceeded'):
                    failure='LOCAL_RUN_RESOURCE_LIMIT'
                elif raw.exit_code in (137,-9):
                    failure='LOCAL_RUN_TERMINATED_UNKNOWN'
                else:
                    failure='LOCAL_RUN_UNAVAILABLE'
                result.update(status='failed',failure=failure)
            elif raw.exit_code != 0:
                result.update(status='failed',failure='LOCAL_RUN_RUNTIME_ERROR')
            else:
                lines=stdout.splitlines(); marker_index=next((i for i in range(len(lines)-1,-1,-1) if lines[i].startswith(OBSERVATION_PREFIX)),None)
                if marker_index is None: raise ValueError('observation missing')
                marker=lines[marker_index]
                payload=json.loads(marker[len(OBSERVATION_PREFIX):])
                result['rawOutput']='\n'.join(lines[:marker_index]+lines[marker_index+1:])[:2048]
                if stdout.endswith('\n') and result['rawOutput']: result['rawOutput']+='\n'
                result.update(status='succeeded',observations=payload['observations'])
                if 'returnValue' in payload: result['returnValue']=payload['returnValue']
        except (ValueError, KeyError, json.JSONDecodeError):
            result.update(status='failed',failure='LOCAL_RUN_OBSERVATION_UNAVAILABLE')
        except InterruptedError:
            result.update(status='interrupted',failure='LOCAL_RUN_STOPPED')
        except Exception as exc:
            result.update(status='failed',failure='LOCAL_RUN_UNAVAILABLE',
                          infrastructureFailureCategory=type(exc).__name__)
        finally:
            try:self._cleanup(self._row(run_id))
            except OSError:
                result['executionStatus']=result.get('status','failed')
                if result.get('failure'):result['executionFailure']=result['failure']
                result.update(status='failed',failure='LOCAL_RUN_CLEANUP_PENDING',cleanupPending=True)
        if datetime.now(timezone.utc).timestamp() >= deadline:
            result.update(status='interrupted',failure='LOCAL_RUN_DEADLINE_EXCEEDED')
        with closing(sqlite3.connect(self.path)) as db:
            changed=db.execute("UPDATE tool_local_tasks SET status=?,owner=NULL,result_json=? WHERE run_id=? AND owner=? AND status='running' AND deadline>?",
                       (result['status'],_json(result),run_id,owner,datetime.now(timezone.utc).timestamp())).rowcount
            if not changed:
                interrupted={**result,'status':'interrupted','failure':'LOCAL_RUN_DEADLINE_EXCEEDED'}
                db.execute("UPDATE tool_local_tasks SET status='interrupted',owner=NULL,result_json=? WHERE run_id=? AND owner=? AND status='running'",
                           (_json(interrupted),run_id,owner))
            db.commit()
        if result.get('cleanupPending'):
            row=self._row(run_id);durable={**result,'status':row['status']}
            with closing(sqlite3.connect(self.path)) as db:
                db.execute('UPDATE tool_local_tasks SET result_json=? WHERE run_id=?',(_json(durable),run_id));db.commit()
        return self._view(self._row(run_id))

    def _cleanup(self,row):
        if row['container_name'] and self.runner is None:
            PublicRunService._remove_container(row['container_name'])
        path=Path(row['workspace']).resolve()
        if path.parent==self.root and path.name==row['run_id'] and path.exists():
            try:shutil.rmtree(path)
            except OSError as exc:raise OSError('workspace cleanup unavailable') from exc

    def cancel(self, run_id):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE tool_local_tasks SET status='cancelled',owner=NULL WHERE run_id=? AND status='running'",(run_id,));db.commit()
        row=self._row(run_id)
        if not row:return {'status':'failed','failure':'LOCAL_RUN_UNAVAILABLE'}
        try:self._cleanup(row)
        except OSError:
            result={**self._view(row),'status':'cancelled','cleanupPending':True}
            with closing(sqlite3.connect(self.path)) as db:
                db.execute('UPDATE tool_local_tasks SET result_json=? WHERE run_id=?',(_json(result),run_id));db.commit()
        return self._view(self._row(run_id))

    def recover(self):
        with closing(sqlite3.connect(self.path)) as db:
            rows=db.execute("SELECT run_id,context_json,workspace,container_name FROM tool_local_tasks WHERE status='running' AND deadline<=?",
                            (datetime.now(timezone.utc).timestamp(),)).fetchall()
            for run_id,context_json,workspace,container_name in rows:
                result={'runId':run_id,'status':'interrupted','failure':'LOCAL_RUN_RECOVERED',**json.loads(context_json)}
                db.execute("UPDATE tool_local_tasks SET status='interrupted',owner=NULL,result_json=? WHERE run_id=?",(_json(result),run_id))
                try:self._cleanup(self._row(run_id))
                except OSError:
                    result['cleanupPending']=True
                    db.execute('UPDATE tool_local_tasks SET result_json=? WHERE run_id=?',(_json(result),run_id))
            db.commit()
        results=[self._view(self._row(row[0])) for row in rows]
        with closing(sqlite3.connect(self.path)) as db:
            pending=db.execute("SELECT run_id FROM tool_local_tasks WHERE json_extract(result_json,'$.cleanupPending')=1").fetchall()
        for (run_id,) in pending:
            try:self._cleanup(self._row(run_id))
            except OSError:continue
            result=self._view(self._row(run_id));result['cleanupPending']=False
            durable_status=self._row(run_id)['status']
            if durable_status=='failed' and result.get('executionStatus'):
                durable_status=result.pop('executionStatus');result['status']=durable_status
            if result.get('executionFailure'):
                result['failure']=result.pop('executionFailure')
            elif result.get('failure')=='LOCAL_RUN_CLEANUP_PENDING' and result.get('terminationReason') in (
                    'wallTimeExceeded','memoryLimitExceeded','outputLimitExceeded'):
                result['failure']='LOCAL_RUN_RESOURCE_LIMIT'
            elif result.get('failure')=='LOCAL_RUN_CLEANUP_PENDING':
                result.pop('failure',None)
            with closing(sqlite3.connect(self.path)) as db:
                db.execute('UPDATE tool_local_tasks SET status=?,result_json=? WHERE run_id=?',(durable_status,_json(result),run_id));db.commit()
            results.append(result)
        return results
