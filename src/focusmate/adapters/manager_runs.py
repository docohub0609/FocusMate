"""Lookup registered tool tasks; never calls run() to recover an uncertain task."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3
from focusmate.contracts.manager import ManagerError, encode

PUBLIC_FIELDS={'runId','status','goalContext','problemRef','codeVersion','loopId','sampleStatus','caseResults','failure','cleanupPending','truncated'}
LOCAL_FIELDS={'runId','status','goalContext','problemRef','codeVersion','loopId','failure','cleanupPending',
              'purpose','observationIntent','sourceRef','setup','call','observe','observations','returnValue',
              'rawOutput','rawOutputTruncated','errorOutput','terminationReason','exitCode'}
CASE_FIELDS={'exampleIndex','testCaseId','outcome','stdin','expectedOutput','actualOutput','stderr','truncated',
             'input','expected','actual','error','stdout','inputTruncated','expectedOutputTruncated','actualOutputTruncated','errorTruncated'}


class ManagerRunLookup:
    def __init__(self,public_service,local_service):
        self.services={'run_public_samples':public_service,'run_local_code':local_service}

    def lookup(self,state,step):
        if step.get('toolContext'):
            step={**step,'roundId':step['toolContext']['loopId']}
        service=self.services.get(step['payload']['toolName'])
        if service is None:return None
        table='tool_public_tasks' if step['payload']['toolName']=='run_public_samples' else 'tool_local_tasks'
        with closing(sqlite3.connect(service.path.as_uri()+'?mode=ro',uri=True)) as db:
            rows=db.execute(f"SELECT run_id,status,context_json,result_json FROM {table} WHERE json_extract(context_json,'$.loopId')=?",(step['roundId'],)).fetchall()
        if not rows:return None
        if len(rows)!=1:raise ManagerError('AMBIGUOUS_RUN')
        run_id,status,context_json,result_json=rows[0];context=json.loads(context_json)
        expected={'loopId':step['roundId'],'goalContext':state['goalContext'],'problemRef':state['problemRef'],'codeVersion':step['codeVersion']}
        if any(context.get(k)!=v for k,v in expected.items()):raise ManagerError('RUN_CONTEXT_MISMATCH')
        raw=json.loads(result_json) if result_json else {'runId':run_id,'status':status,**expected}
        if table=='tool_local_tasks':raw.setdefault('loopId',context['loopId'])
        if any(raw.get(k)!=v for k,v in expected.items()) or raw.get('runId')!=run_id or raw.get('status')!=status:raise ManagerError('RUN_RESULT_MISMATCH')
        if status=='succeeded':
            if table=='tool_public_tasks':
                cases=raw.get('caseResults')
                if raw.get('sampleStatus') not in ('passed','failed') or not isinstance(cases,list) or not 1<=len(cases)<=8:
                    raise ManagerError('INVALID_PUBLIC_RESULT')
                if any(not isinstance(c,dict) or not c.get('outcome') for c in cases) or raw['sampleStatus']=='passed' and any(c['outcome']!='passed' for c in cases):
                    raise ManagerError('INVALID_PUBLIC_RESULT')
            else:
                args=step['payload']['arguments']
                if any(k in raw for k in ('verdict','expectedOutput','correct','sampleStatus')) or not isinstance(raw.get('observations'),dict):raise ManagerError('INVALID_LOCAL_RESULT')
                if set(raw['observations'])!=set(args['observe']['identifiers']) or ('returnValue' in raw)!=args['observe']['returnValue'] or raw.get('setup')!=args['initialVariables']:
                    raise ManagerError('INVALID_LOCAL_RESULT')
        fields=PUBLIC_FIELDS if table=='tool_public_tasks' else LOCAL_FIELDS
        data={k:deepcopy(v) for k,v in raw.items() if k in fields}
        if 'caseResults' in data:
            data['caseResults']=[{k:v for k,v in case.items() if k in CASE_FIELDS} for case in data['caseResults']]
        if len(encode(data).encode())>16384:raise ManagerError('RUN_RESULT_TOO_LONG')
        return data

    def cancel(self,state,step):
        data=self.lookup(state,step)
        if data is None:return {'status':'pending','reason':'TASK_NOT_REGISTERED'}
        service=self.services[step['payload']['toolName']]
        if data['status'] not in ('waiting','running') and not data.get('cleanupPending'):
            return {'status':'done','runId':data['runId'],'taskStatus':data['status']}
        result=service.cancel(data['runId'])
        return {'status':'pending' if result.get('cleanupPending') else 'done','runId':data['runId'],
                'taskStatus':result.get('status'),'cleanupPending':bool(result.get('cleanupPending'))}
