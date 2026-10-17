"""Read selected, pinned business records without migrations or Core imports."""
from contextlib import ExitStack, closing
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from jsonschema.exceptions import ValidationError

from focusmate.contracts.observations import canonical, read_observation
from focusmate.contracts.raw_events import read_event
from focusmate.contracts.progress import ProgressSourceError


class SQLiteProgressResolver:
    """One read transaction per database. A missing source is never synthesized."""

    def __init__(self, path, request, *, public_task_db=None, supplemental_records=()):
        self.path=Path(path); self.request=request; self.public_path=public_task_db
        self.supplemental=deepcopy(supplemental_records)
        self.stack=ExitStack(); self.db=None; self.public=None; self.error=None;self.cache={}

    def __enter__(self):
        try:
            self.db=self._open(self.path)
            if self.public_path:self.public=self._open(Path(self.public_path))
            for source in self.request.get('sources',[]):
                self.cache[source['ref']]=self._resolve(source['ref'])
        except sqlite3.OperationalError as error:
            self.error='DATABASE_TABLE_UNAVAILABLE' if 'no such table' in str(error) else 'DATABASE_UNAVAILABLE';self.cache={}
        except ValidationError:
            self.error='SOURCE_CONTENT_INVALID';self.cache={}
        except (OSError,sqlite3.Error,ValueError,KeyError,TypeError,AttributeError):
            self.error='SOURCE_UNAVAILABLE';self.cache={}
        finally:self.stack.close()
        return self

    def __exit__(self,*args):self.stack.close()

    def _open(self,path):
        db=self.stack.enter_context(closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)))
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        return db

    @staticmethod
    def _one(db,query,args):
        row=db.execute(query,args).fetchone()
        if row is None:raise ValueError('SOURCE_UNAVAILABLE')
        return row

    def _code(self,source_id):
        row=self._one(self.db,'SELECT * FROM code_snapshots WHERE code_snapshot_id=? AND goal_id=?',
            (source_id,self.request['goalContext']['goalId']))
        if sha256(row['source_code'].encode()).hexdigest()!=row['content_hash'].removeprefix('sha256:'):
            raise ValueError('STORED_CODE_HASH_MISMATCH')
        return row

    def _code_ref(self,version):
        matches=[]
        for s in self.request['sources']:
            if s['sourceType']=='code' and self._code(s['sourceId'])['code_version']==version:matches.append(s['ref'])
        if len(matches)!=1:raise ValueError('CODE_SOURCE_REQUIRED')
        return matches[0]

    def resolve(self,ref):
        if self.error:raise ProgressSourceError(self.error)
        try:return deepcopy(self.cache[ref])
        except KeyError:raise ValueError('SOURCE_UNAVAILABLE') from None

    def _resolve(self,ref):
        if self.error or self.db is None:raise ValueError('DATABASE_UNAVAILABLE')
        req=self.request; context=req['goalContext'];goal=context['goalId'];problem=req['problemRef']
        row=self._one(self.db,'SELECT problem_id,problem_version FROM goal_records WHERE goal_id=?',(goal,))
        if tuple(row)!=(problem['problemId'],problem['problemVersion']):raise ValueError('GOAL_PROBLEM_MISMATCH')
        sources=[s for s in req['sources'] if s['ref']==ref]
        if len(sources)!=1:raise ValueError('DUPLICATE_REFERENCE')
        source=sources[0];typ=source['sourceType'];source_id=source['sourceId'];version=source['version']
        expected={'contract':'goal','problem':'problemFact','code':'code','observation':'observation','oj':'runEvidence','publicRun':'runEvidence'}
        if typ!='external' and source['kind']!=expected[typ]:raise ValueError('SOURCE_TYPE_MISMATCH')
        if typ=='external':
            records=[r for r in self.supplemental if r.get('ref')==source_id]
            if len(records)!=1 or source_id!=ref or source['kind'] not in ('dialogue','hypothesis'):
                raise ValueError('EXTERNAL_SOURCE_INVALID')
            record=deepcopy(records[0]);record['origin']='developer-supplied'
            return record
        extra={}
        if typ=='contract':
            row=self._one(self.db,'SELECT status,compilation_json FROM goal_contract_versions WHERE goal_id=? AND version=?',(goal,version))
            content=json.loads(row['compilation_json'])['contract']
            if row['status'] not in ('frozen','superseded') or content['contractStatus'] not in ('frozen','superseded'):
                raise ValueError('CONTRACT_NOT_FROZEN')
            # The locator is the Goal ID; version selects its immutable contract.
            if source_id!=goal:raise ValueError('CONTRACT_LOCATOR_MISMATCH')
        elif typ=='code':
            row=self._code(source_id);version=row['code_version']
            content={'snapshotRef':ref,'language':row['language'],'text':row['source_code']}
        elif typ=='problem':
            if source_id!=problem['problemId'] or version!=problem['problemVersion']:raise ValueError('PROBLEM_MISMATCH')
            row=self._one(self.db,'SELECT statement,input_spec,output_spec,constraints_text,lifecycle_status FROM problem_versions WHERE problem_id=? AND version=?',(source_id,version))
            if row['lifecycle_status']!='frozen':raise ValueError('PROBLEM_NOT_FROZEN')
            examples=self.db.execute('SELECT input_text,output_text FROM problem_examples WHERE problem_id=? AND version=? ORDER BY example_index',(source_id,version))
            content={'statement':row['statement'],'inputSpec':row['input_spec'],'outputSpec':row['output_spec'],
                'constraints':row['constraints_text'],'examples':[{'input':r[0],'output':r[1]} for r in examples]}
        elif typ=='observation':
            row=self._one(self.db,'SELECT observation_json FROM observations WHERE observation_id=? AND goal_id=?',(source_id,goal))
            observation=read_observation(json.loads(row[0]));version=1
            if observation['observationId']!=source_id or observation['goalContext']!=context:raise ValueError('OBSERVATION_MISMATCH')
            for event_id in observation['sourceEventIds']:
                eventrow=self._one(self.db,'SELECT event_json FROM raw_events WHERE event_id=? AND goal_id=?',(event_id,goal))
                event=read_event(json.loads(eventrow[0]))
                if event['goalContext']!=context:raise ValueError('EVENT_SCOPE_MISMATCH')
                scope=observation['organization']['scope']
                for key in ('contractVersion','sessionRef'):
                    if event.get(key) is not None and event[key]!=scope.get(key):raise ValueError('EVENT_ASSOCIATION_MISMATCH')
                payload=event['payload']
                if payload.get('problemRef',problem)!=problem:raise ValueError('EVENT_PROBLEM_MISMATCH')
                if 'codeVersion' in payload:self._code_ref(payload['codeVersion'])
            evidence=observation['evidence'];links=[]
            for identifier in evidence.get('codeRefs',[]):
                code=self._code(identifier);links.append(self._code_ref(code['code_version']))
            if evidence.get('codeVersion') is not None:links.append(self._code_ref(evidence['codeVersion']))
            content={'observation':observation,'codeRefs':list(dict.fromkeys(links))}
            extra={k:v for k,v in observation['organization']['scope'].items() if k!='goalContext' and v is not None}
        elif typ=='oj':
            row=self._one(self.db,'''SELECT r.oj_result_id,r.submission_id,r.goal_id,r.problem_id,r.problem_version,r.code_version,r.verdict,
                s.status,s.goal_id AS sg,s.code_version AS sc,s.problem_id AS sp,s.problem_version AS sv
                FROM oj_results r JOIN submissions s ON s.submission_id=r.submission_id WHERE r.oj_result_id=? AND r.goal_id=?''',(source_id,goal))
            if (row['sg'],row['sp'],row['sv'],row['sc'])!=(goal,problem['problemId'],problem['problemVersion'],row['code_version']) or row['status']!='finished':raise ValueError('OJ_ASSOCIATION_MISMATCH')
            if (row['problem_id'],row['problem_version'])!=(problem['problemId'],problem['problemVersion']) or row['verdict'] not in ('AC','WA','TLE','RE','CE'):raise ValueError('OJ_RESULT_INVALID')
            version=1
            content={'evidenceType':'ojResult','codeSnapshotRef':self._code_ref(row['code_version']),
                'completed':True,'submissionId':row['submission_id'],'verdict':row['verdict']}
        elif typ=='publicRun':
            if self.public is None:raise ValueError('PUBLIC_DATABASE_REQUIRED')
            row=self._one(self.public,'SELECT context_json,result_json,status FROM tool_public_tasks WHERE run_id=?',(source_id,))
            saved=json.loads(row['context_json']);result=json.loads(row['result_json']) if row['result_json'] else None
            if saved['goalContext']!=context or saved['problemRef']!=problem:raise ValueError('RUN_SCOPE_MISMATCH')
            version=1
            content={'evidenceType':'publicSampleRunResult','codeSnapshotRef':self._code_ref(saved['codeVersion']),
                'completed':False,'runId':source_id,'status':row['status']}
            if result is not None:
                if any(result.get(k)!=saved.get(k) for k in ('goalContext','problemRef','codeVersion','loopId')) or result.get('runId')!=source_id or result.get('status')!=row['status']:raise ValueError('RUN_RESULT_MISMATCH')
                content['completed']=row['status']=='succeeded' and result.get('sampleStatus') in ('passed','failed')
                content['sampleStatus']=result.get('sampleStatus')
                fields=('testCaseId','outcome','input','expectedOutput','actualOutput','error','terminationReason','exitCode',
                    'inputTruncated','expectedOutputTruncated','actualOutputTruncated','errorTruncated')
                content['caseResults']=[{k:item[k] for k in fields if k in item} for item in result.get('caseResults',[])]
            comparison=self.public.execute('SELECT conditions_hash FROM tool_public_comparison WHERE run_id=?',(source_id,)).fetchone()
            if comparison:content['conditionsHash']=comparison[0]
        else:raise ValueError('SOURCE_TYPE_INVALID')
        return {'ref':ref,'kind':source['kind'],'version':version,'goalContext':deepcopy(context),
            'problemRef':deepcopy(problem),'content':content,'contentHash':'sha256:'+sha256(canonical(content).encode()).hexdigest(),
            'origin':'business-database','sourceId':source_id,**extra}
