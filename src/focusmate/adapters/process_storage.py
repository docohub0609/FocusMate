"""Read pinned business evidence. No Core orchestration imports or migrations."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from focusmate.adapters.progress_storage import SQLiteProgressResolver
from focusmate.adapters.manager_runs import LOCAL_FIELDS


def digest(value):
    return 'sha256:'+sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


class SQLiteProcessResolver(SQLiteProgressResolver):
    def __init__(self,*args,local_task_db=None,**kwargs):
        super().__init__(*args,**kwargs);self.local_path=local_task_db

    def _resolve(self,ref):
        original=self.request
        source=next(s for s in original['sources'] if s['ref']==ref)
        typ=source['sourceType'];goal=original['goalContext']['goalId']
        ctx={'goalId':goal,'scope':'goal'}
        goal_row=self._one(self.db,'SELECT problem_id,problem_version FROM goal_records WHERE goal_id=?',(goal,))
        if tuple(goal_row)!=(original['problemRef']['problemId'],original['problemRef']['problemVersion']):raise ValueError('GOAL_PROBLEM_MISMATCH')
        if typ=='contract':
            if source['sourceId']!=goal or source['kind']!='goal':raise ValueError('CONTRACT_LOCATOR_MISMATCH')
            row=self._one(self.db,'SELECT status,compilation_json FROM goal_contract_versions WHERE goal_id=? AND version=?',(goal,source['version']))
            full=json.loads(row['compilation_json'])
            if row['status']!='frozen':raise ValueError('CURRENT_FROZEN_CONTRACT_REQUIRED')
            content={k:full[k] for k in ('contract','rubrics')}
            return {'ref':ref,'kind':'goal','version':source['version'],'goalContext':ctx,'problemRef':original['problemRef'],
                'content':content,'contentHash':digest(content),'origin':'business-database','sourceId':goal}
        if typ in ('publicRun','localRun'):
            table='tool_public_tasks' if typ=='publicRun' else 'tool_local_tasks'
            db=self.public if typ=='publicRun' else self._open(Path(self.local_path)) if self.local_path else None
            if db is None:raise ValueError('RUN_DATABASE_REQUIRED')
            row=self._one(db,f'SELECT context_json,result_json,status FROM {table} WHERE run_id=?',(source['sourceId'],))
            saved=json.loads(row['context_json']);ctx=saved['goalContext']
            if ctx['goalId']!=goal or ctx.get('scope')=='subgoal' and ctx!=original['goalContext']:raise ValueError('RUN_SCOPE_MISMATCH')
            if saved['problemRef']!=original['problemRef']:raise ValueError('RUN_PROBLEM_MISMATCH')
            if typ=='localRun':
                raw=json.loads(row['result_json']) if row['result_json'] else None
                content={'evidenceType':'targetedRunResult','codeSnapshotRef':self._code_ref(saved['codeVersion']),
                    'runId':source['sourceId'],'status':row['status'],'completed':False}
                if raw:
                    if (any(raw.get(k)!=saved[k] for k in ('goalContext','problemRef','codeVersion'))
                            or raw.get('runId')!=source['sourceId'] or raw.get('status')!=row['status']):raise ValueError('RUN_RESULT_MISMATCH')
                    if saved.get('sourceRef')!=raw.get('sourceRef'):raise ValueError('LOCAL_SOURCE_MISMATCH')
                    if row['status']=='succeeded' and (not isinstance(raw.get('observations'),dict) or raw.get('setup')!=saved['request']['initialVariables']):raise ValueError('LOCAL_OBSERVATION_MISMATCH')
                    content.update({k:deepcopy(v) for k,v in raw.items() if k in LOCAL_FIELDS})
                    content['completed']=row['status']=='succeeded'
                    content['generatedSourceRef']={k:raw.get('generatedSourceRef',{}).get(k) for k in ('templateVersion','contentHash')}
                return {'ref':ref,'kind':'runEvidence','version':1,'goalContext':ctx,'problemRef':original['problemRef'],
                    'content':content,'contentHash':digest(content),'origin':'business-database','sourceId':source['sourceId']}
        elif typ=='observation':
            row=self._one(self.db,'SELECT observation_json FROM observations WHERE observation_id=? AND goal_id=?',(source['sourceId'],goal))
            ctx=json.loads(row[0])['goalContext']
            if ctx['goalId']!=goal or ctx.get('scope')=='subgoal' and ctx!=original['goalContext']:raise ValueError('OBSERVATION_SCOPE_MISMATCH')
        try:
            self.request={**original,'goalContext':ctx}
            return super()._resolve(ref)
        finally:self.request=original
