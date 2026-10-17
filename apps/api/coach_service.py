"""Application boundary: authoritative business snapshots and the Coach service."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

# App entry points also run directly from a checkout without an editable installation.
SOURCE_ROOT=str(Path(__file__).resolve().parents[2]/'src')
if SOURCE_ROOT not in sys.path:sys.path.insert(0,SOURCE_ROOT)

from apps.api.editor_storage import EditorRepository
from apps.api.goal_contract_storage import GoalContractRepository, load_inputs
from apps.api.submission_storage import SubmissionRepository
from focusmate.adapters.coach_events import TABLES
from focusmate.adapters.manager_storage import SQLiteManagerStore
from focusmate.adapters.final_oj import SQLiteFinalOJStore
from focusmate.core.dynamic_coach import DynamicCoachManager
from focusmate.core.final_oj import GoalStateManager
from focusmate.core.context_builder import hash_content
from focusmate.core.runtime_subgoals import current_goals
from focusmate.contracts.manager import ManagerError
from services.oj_worker.judge import OJResultRepository


class CoachService:
    def __init__(self,path,*,manager_path=None):
        self.path=Path(path).resolve()
        self.editor=EditorRepository(self.path)
        self.contracts=GoalContractRepository(self.path)
        self.manager_path=Path(manager_path or self.path.with_name(self.path.stem+'-coach.sqlite3'))
        self.manager=DynamicCoachManager(SQLiteManagerStore(self.manager_path))
        self.final=GoalStateManager(SQLiteFinalOJStore(self.path))
        with closing(self.connect()) as db,db:
            for statement in TABLES: db.execute(statement)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        return db

    @staticmethod
    def current_code(db,goal_id):
        draft=db.execute('SELECT code_version FROM code_drafts WHERE goal_id=?',(goal_id,)).fetchone()
        if draft is not None: return draft[0]
        return db.execute('SELECT MAX(code_version) FROM code_snapshots WHERE goal_id=?',(goal_id,)).fetchone()[0]

    def workspace(self,goal_id,*,create=False):
        goal=self.editor.submissions.get_goal(goal_id)
        if create:
            contract=self.contracts.ensure_minimal(goal_id)
            session='coach-'+hashlib.sha256((goal_id+':'+str(contract['version'])).encode()).hexdigest()[:32]
            with closing(self.connect()) as db,db:
                db.execute('BEGIN IMMEDIATE')
                current=self.current_code(db,goal_id)
                try:self.manager.create(session,{'goalId':goal_id,'scope':'goal'},goal['problemRef'],
                    contract_version=contract['version'],code_version=current)
                except ManagerError as exc:
                    if str(exc)!='SESSION_EXISTS':raise
                old=db.execute('SELECT session_id FROM coach_workspaces WHERE goal_id=?',(goal_id,)).fetchone()
                if old and old[0]!=session:
                    self.manager.command(old[0],'binding:'+str(contract['version']),'binding_changed',{})
                db.execute('''INSERT INTO coach_workspaces VALUES(?,?,?,?,?,0)
                    ON CONFLICT(goal_id) DO UPDATE SET contract_version=excluded.contract_version,session_id=excluded.session_id''',
                    (goal_id,contract['version'],session,current,'[]'))
        with closing(self.connect()) as db:
            binding=db.execute('SELECT * FROM coach_workspaces WHERE goal_id=?',(goal_id,)).fetchone()
            if not binding:raise ManagerError('COACH_WORKSPACE_NOT_FOUND')
            row=db.execute('SELECT compilation_json FROM goal_contract_versions WHERE goal_id=? AND version=?',
                           (goal_id,binding['contract_version'])).fetchone()
            contract=json.loads(row[0])['contract']
        self.synchronize(binding['session_id'])
        state=self.read(binding['session_id'])
        return {'goal':goal,'contract':contract,'session':{'sessionId':state['sessionId'],'status':state['status']},
                'workGoals':state['workGoals'],'goalState':self.final.goal_state(goal_id)}

    def _sync(self,db,session):
        state=self.manager.read(session);goal=state['goalContext']['goalId']
        binding=db.execute('SELECT * FROM coach_workspaces WHERE goal_id=?',(goal,)).fetchone()
        frozen=db.execute("SELECT version FROM goal_contract_versions WHERE goal_id=? AND status='frozen'",(goal,)).fetchone()
        if not binding or binding['session_id']!=session or not frozen or frozen[0]!=state['contractVersion']:
            self.manager.command(session,'binding-invalid','binding_changed',{})
            raise ManagerError('COACH_BINDING_CHANGED')
        code=self.current_code(db,goal)
        if state['codeVersion']!=code:
            # Revision is part of the key so clearing and restoring a version cannot reuse a previous command.
            self.manager.command(session,'sync:'+str(state['revision']),'code_changed',{'codeVersion':code})
        completion=db.execute('SELECT state_json FROM goal_states WHERE goal_id=?',(goal,)).fetchone()
        if completion and json.loads(completion[0])['status']=='completed':
            result=json.loads(completion[0])['lastVerifierResultId']
            self.manager.command(session,'final:'+result,'final_completed',{'resultId':result})
        return self.manager.read(session)

    def _project(self,db,state):
        focused=set(self._current_targets(state))
        targets=[{'subgoalId':g['subgoalId'],'version':g['version']} for g in current_goals(state['workGoals'])
                 if g['subgoalId'] in focused and g['status'] in ('active','verified')]
        db.execute('UPDATE coach_workspaces SET code_version=?,targets_json=?,paused=? WHERE session_id=?',
            (state['codeVersion'],json.dumps(targets),int(state['status']=='paused' or state['completed']),state['sessionId']))

    def synchronize(self,session):
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            state=self._sync(db,session);self._project(db,state)
            return state

    def command(self,session,request):
        if (not isinstance(request,dict) or set(request)!={'operationId','kind','codeVersion','payload'}
                or request['kind'] not in {'ask','message','check','pause','resume','retry','restart','grant_permission','stop_run','revoke_execution'}):
            raise ManagerError('INVALID_COACH_COMMAND')
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE');self._sync(db,session)
            result=self.manager.command(session,request['operationId'],request['kind'],request['payload'],
                expected_code_version=request['codeVersion'])
            self._project(db,self.manager.read(session))
            return result

    def read(self,session,*,limit=20,cursor=0,goal_cursor=None):
        if type(limit) is not int or not 1<=limit<=100 or type(cursor) is not int or cursor<0:
            raise ManagerError('INVALID_HISTORY_PAGE')
        # Read the receipt and Manager state under the same business lock used by
        # process_events. A consumed receipt cannot be paired with the older round.
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE')
            state=self._sync(db,session);self._project(db,state)
            pending_follow_up=bool(db.execute('SELECT 1 FROM coach_execution_links WHERE session_id=? AND processed=0 LIMIT 1',(session,)).fetchone())
        r=state['rounds'].get(state['currentRoundId'])
        pending=next((x for x in state['steps'].values() if r and r.get('permissionTools') and
            x['roundId']==r['roundId'] and x['kind']=='tool' and x['status']=='pending'),None)
        history=[{'kind':'message',**deepcopy(x),'occurredAt':x['receivedAt']} for x in state['messages']]
        history += [{'kind':'action','actionId':x['actionId'],'roundId':x['roundId'],'codeVersion':x['codeVersion'],
            'content':x['learningAction']['content'],'subgoalIds':x['learningAction']['subgoalIds'],
            'followUp':deepcopy(x['learningAction'].get('followUp')),
            'workGoalVersions':x['workGoalVersions'],'currentCode':x['codeVersion']==state['codeVersion'],
            'historySequence':x.get('historySequence'),'occurredAt':x.get('publishedAt',0),
            'evidenceRefs':x['learningAction']['evidenceRefs']} for x in state['outbox']]
        history.sort(key=lambda x:x['occurredAt'])
        for index,item in enumerate(history,1):
            if not item.get('historySequence'):item['historySequence']=index
            item['historySequence'] *= 1024
        # Reserve slots before a legacy round's first published action. New reports
        # carry their own saved sequence; old records are read without migration.
        for report in state['checks'].values():
            item=self._report(state,report)
            owner=next((x for x in state['rounds'].values() if report['reportId'] in x.get('reportRefs',[])),None)
            item.update((owner or {}).get('reportEvents',{}).get(report['reportId'],{}))
            item['roundId']=item['roundId'] or (owner['roundId'] if owner else None)
            if item['historySequence'] is not None:item['historySequence'] *= 1024
            else:
                report_index=(owner or {}).get('reportRefs',[]).index(report['reportId']) if owner else 0
                after=next((x['historySequence'] for x in history if x.get('roundId')==item['roundId'] and x['kind']=='action'),None)
                if after is None:
                    before=[x['historySequence'] for x in history if x.get('roundId')==item['roundId']]
                    after=max(before,default=0)+1024
                item['historySequence']=after-512+report_index
            history.append(item)
        history.sort(key=lambda x:x['historySequence'],reverse=True)
        if cursor:history=[x for x in history if x['historySequence']<cursor]
        from .coach_presentation import source_ranges,round_failure,check_summary,action_content,goal_projection
        from .repair_presentation import safe_repair
        from focusmate.core.dynamic_coach_recovery import retry_availability
        for item in history[:limit]:
            item.setdefault('codeRanges',[])
            item.setdefault('codeHash',None)
            if item['kind']=='action':
                original_round=state['rounds'].get(item['roundId'])
                item['checkSummary']=check_summary(original_round,state['workGoals'],state['checks'],state['steps'])
                item['content']=action_content(item['content'],item['checkSummary'])
            if item['kind']=='action' and item['codeVersion'] is not None:
                code=self.editor.submissions.get_code_snapshot(state['goalContext']['goalId'],item['codeVersion'])
                item['codeRanges']=source_ranges(code['sourceCode'],item['content'])
                item['codeHash']=code['contentHash']
        goal_history=[{'id':key+'@'+str(v['version']),'subgoalId':key,'currentStatus':g['status'],
            'currentVersion':g['versions'][-1]['version'],'version':deepcopy(v),
            'verificationRefs':[ref for ref in g['verificationRefs'] if state['checks'].get(ref,{}).get('workGoalVersion')==v['version']]}
            for key,g in state['workGoals'].items() for v in g['versions']]
        goal_history.sort(key=lambda x:(x['version']['createdAt'],x['id']),reverse=True)
        if goal_cursor is not None:
            found=next((i for i,x in enumerate(goal_history) if x['id']==goal_cursor),None)
            if found is None:raise ManagerError('INVALID_GOAL_HISTORY_CURSOR')
            goal_history=goal_history[found+1:]
        return {'sessionId':session,'goalContext':state['goalContext'],'contractVersion':state['contractVersion'],
            'codeVersion':state['codeVersion'],'status':state['status'],
            'pendingFollowUp':pending_follow_up,
            'pendingPermission':{'stepId':pending['stepId'],'roundId':r['roundId'],'codeVersion':pending['codeVersion'],
                'toolName':pending['payload']['toolName'],'arguments':deepcopy(pending['payload']['arguments'])} if pending else None,
            'round':{**{k:r.get(k) for k in ('roundId','status','phase','failure','counts','batchId','uncheckedTargets')},
                     'repair':safe_repair(r.get('repairRecord')),
                     'failureInfo':round_failure(r,state['limits']['model']),
                     'retryInfo':retry_availability(state,r,self.manager.clock()),
                     'checkSummary':check_summary(r,state['workGoals'],state['checks'],state['steps'])} if r else None,
            'workGoals':goal_projection(state,current_goals(state['workGoals'])), 'currentWorkGoalIds':self._current_targets(state),
            'messages':deepcopy(state['messages'][-limit:]),
            'workGoalHistory':goal_history[:limit],
            'goalHistoryNextCursor':goal_history[limit-1]['id'] if len(goal_history)>limit else None,
            'items':history[:limit], 'nextCursor':history[limit-1]['historySequence'] if len(history)>limit else None,
            'goalState':self.final.goal_state(state['goalContext']['goalId'])}

    @staticmethod
    def _current_targets(state):
        if 'currentWorkGoalIds' in state:
            return [key for key in state['currentWorkGoalIds'] if state['workGoals'].get(key,{}).get('status') not in ('withdrawn','replaced')]
        r=state['rounds'].get(state['currentRoundId']) or {}
        if 'acceptedPlan' in r:
            ids=r['acceptedPlan']['action']['subgoalIds']
        elif r.get('targets'):
            ids=[t['subgoalId'] for t in r['targets']]
        else:
            ids=state['outbox'][-1]['learningAction']['subgoalIds'] if state['outbox'] else []
        return [key for key in ids if state['workGoals'].get(key,{}).get('status')=='active']

    def _report(self,state,report):
        from .coach_presentation import safe_report,source_ranges
        def source(version):
            try:return self.editor.submissions.get_code_snapshot(state['goalContext']['goalId'],version)['sourceCode']
            except ValueError:return None
        item=safe_report(report,state=state,source_for_version=source)
        item.update(codeHash=None,codeRanges=[])
        if item['codeVersion'] is not None:
            code=self.editor.submissions.get_code_snapshot(state['goalContext']['goalId'],item['codeVersion'])
            item['codeHash']=code['contentHash']
            for check in item['checks']:
                for quote in check['quotes']:
                    if quote['ref']=='code:'+str(item['codeVersion']) and quote['text']:
                        for matched in source_ranges(code['sourceCode'],'```python\n'+quote['text']+'\n```'):
                            if matched not in item['codeRanges']:item['codeRanges'].append(matched)
        return item

    def work_goal(self,session,subgoal_id,*,version=None,limit=20,cursor=0):
        if (type(limit) is not int or not 1<=limit<=100 or type(cursor) is not int or cursor<0
                or version is not None and (type(version) is not int or version<1)):
            raise ManagerError('INVALID_HISTORY_PAGE')
        state=self.synchronize(session)
        goal=state['workGoals'].get(subgoal_id)
        if not goal:raise ManagerError('WORK_GOAL_NOT_FOUND')
        selected=next((v for v in goal['versions'] if v['version']==(version or goal['versions'][-1]['version'])),None)
        if not selected:raise ManagerError('WORK_GOAL_VERSION_NOT_FOUND')
        from .coach_presentation import source_ranges,check_summary,action_content
        items=[]
        for report in state['checks'].values():
            if (report['verifierResult']['goalContext'].get('subgoalId')==subgoal_id
                    and report['workGoalVersion']==selected['version']):
                item=self._report(state,report)
                # Detail history uses the original ISO timestamps consistently.
                item['occurredAt']=report['verifierResult']['createdAt']
                items.append(item)
        for action in state['outbox']:
            if action['workGoalVersions'].get(subgoal_id)!=selected['version']:continue
            summary=check_summary(state['rounds'].get(action['roundId']),state['workGoals'],state['checks'],state['steps'])
            content=action_content(action['learningAction']['content'],summary)
            code=self.editor.submissions.get_code_snapshot(state['goalContext']['goalId'],action['codeVersion']) if action['codeVersion'] else None
            items.append({'kind':'action','actionId':action['actionId'],'codeVersion':action['codeVersion'],
                'content':content,'codeHash':code['contentHash'] if code else None,
                'codeRanges':source_ranges(code['sourceCode'],content) if code else [],
                'checkSummary':summary,'occurredAt':action['learningAction']['createdAt']})
        items.sort(key=lambda item:(item['occurredAt'],item.get('reportId',item.get('actionId'))),reverse=True)
        # The cursor names the last immutable item, represented by its index only within
        # this version's stable prefix; use an append count boundary to survive new items.
        for index,item in enumerate(reversed(items),1):item['historySequence']=index
        if cursor:items=[item for item in items if item['historySequence']<cursor]
        return {'goalContext':{'goalId':state['goalContext']['goalId'],'scope':'subgoal','subgoalId':subgoal_id},
            'status':goal['status'],'currentVersion':goal['versions'][-1]['version'],'version':deepcopy(selected),
            'items':items[:limit], 'nextCursor':items[limit-1]['historySequence'] if len(items)>limit else None}

    def completion(self,goal_id):
        """Read final evidence without creating a contract, session, or model round."""
        self.editor.submissions.get_goal(goal_id)
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            current=self.current_code(db,goal_id)
            row=db.execute('SELECT state_json FROM goal_states WHERE goal_id=?',(goal_id,)).fetchone()
            state=json.loads(row[0]) if row else None
            verified=None
            if state and state['status']=='completed':
                row=db.execute('SELECT result_json FROM final_verifier_results WHERE goal_id=? AND result_id=?',
                    (goal_id,state['lastVerifierResultId'])).fetchone()
                verified=json.loads(row[0]) if row else None
            has_results=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='oj_results'").fetchone()
            pending=db.execute('''SELECT 1 FROM oj_results o LEFT JOIN coach_final_receipts c ON c.result_id=o.oj_result_id
                WHERE o.goal_id=? AND o.verdict='AC' AND c.result_id IS NULL LIMIT 1''',(goal_id,)).fetchone() if has_results else None
            row=db.execute('SELECT report_json FROM final_oj_requests WHERE goal_id=? ORDER BY rowid DESC LIMIT 1',(goal_id,)).fetchone()
            report=json.loads(row[0]) if row else None
        completed=bool(verified and verified['status']=='passed')
        return {'goalId':goal_id,'currentCodeVersion':current,
            'status':'completed' if completed else 'pending' if pending else 'notVerified',
            'codeVersion':verified['inputVersionRefs'].get('codeVersion') if completed else None,
            'codeHash':verified['inputVersionRefs'].get('codeHash') if completed else None,
            'resultId':verified['verifierResultId'] if completed else None,
            'report':{k:report[k] for k in ('status','reason','inputVersionRefs')} if report else None}

    def check_batch(self,session,batch_id):
        from .coach_presentation import check_summary
        state=self.synchronize(session)
        r=next((r for r in state['rounds'].values() if r.get('batchId')==batch_id),None)
        if not r:raise ManagerError('CHECK_BATCH_NOT_FOUND')
        return {'batchId':batch_id,'status':r['status'],'uncheckedTargets':r.get('uncheckedTargets',r.get('checkQueue',[])),
            'checkSummary':check_summary(r,state['workGoals'],state['checks'],state['steps']),
            'reports':[self._report(state,state['checks'][x]) for x in r['reportRefs']]}

    def materials(self,session):
        s=self.synchronize(session);goal_id=s['goalContext']['goalId']
        _,problem,_=load_inputs(self.path,goal_id)
        with closing(self.connect()) as db:
            row=db.execute('SELECT compilation_json FROM goal_contract_versions WHERE goal_id=? AND version=?',
                           (goal_id,s['contractVersion'])).fetchone()
            contract=json.loads(row[0])['contract']
        records=[]
        def add(ref,kind,content,version=1,scope=None):
            records.append({'ref':ref,'kind':kind,'version':version,'content':deepcopy(content),
                'contentHash':hash_content(content),'goalContext':scope or s['goalContext'],
                'problemRef':s['problemRef'],'contractVersion':s['contractVersion']})
        add('contract','goal',contract,s['contractVersion'])
        add('problem','problemFact',problem['problemPackage'],s['problemRef']['problemVersion'])
        for m in s['messages'][-20:]:
            add('message:'+str(m['sequence']),'dialogue',{'role':'user','text':m['text'],
                'codeSnapshotRef':'code:'+str(m['codeVersion']) if m.get('codeVersion') else None},m['sequence'])
        current=None;code=None
        if s['codeVersion'] is not None:
            code=self.editor.submissions.get_code_snapshot(goal_id,s['codeVersion'])
            if hashlib.sha256(code['sourceCode'].encode()).hexdigest()!=code['contentHash']:raise ManagerError('CODE_HASH_MISMATCH')
            current='code:'+str(s['codeVersion'])
            add(current,'code',{'snapshotRef':current,'text':code['sourceCode'],'language':'python3'},s['codeVersion'])
        # A later question needs the saved outcomes too; the trigger alone only
        # describes the one event which happened to start an automatic round.
        from .coach_evidence import saved_execution_evidence,relevant_check_reports
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            for ref,kind,content in saved_execution_evidence(db,s,code):
                add(ref,kind,content,s['codeVersion'])
        for ref,report in relevant_check_reports(s):
            projected={k:v for k,v in report.items() if k!='inputSnapshot'}
            add(ref,'verifiedFact',projected,report['workGoalVersion'],report['verifierResult']['goalContext'])
        historical=[]
        previous=sorted((old for old in s['rounds'].values()
            if old['roundId']!=s['currentRoundId'] and 'diagnosis' in old),key=lambda old:old['createdAt'],reverse=True)[:2]
        for old in previous:
            report={k:v for k,v in old['diagnosis'].items() if k!='inputSnapshot'}
            ref='diagnosis:'+old['roundId'];historical.append(ref)
            add(ref,'hypothesis',{'diagnosisReport':report,'semanticStatus':'modelOpinionNotVerified'})
        from focusmate.core.skill_sections import load_index
        root=Path(__file__).resolve().parents[2]
        catalog=[]
        for pin in json.loads((root/'skills/diagnosis-enabled.json').read_text(encoding='utf-8')):
            index=load_index(root/pin['path'],pin['version'],pin['hash'])
            catalog.append({'skillId':index.skill_id,'version':index.version,'hash':index.hash,'path':str(index.package_path),
                'sections':[{'sectionId':x.section_id,'description':x.description} for x in index.sections]})
        return {'records':records,'skillCatalog':catalog,'historyDiagnosisRefs':historical,'currentCodeRef':current,'contractRef':'contract',
                'codeVersion':s['codeVersion'],'contractVersion':s['contractVersion'],'messageSeq':len(s['messages']),
                'userRequestRef':'message:'+str(s['messages'][-1]['sequence']) if s['messages'] else None}

    def complete(self,session,step_id,token,result):
        # Keep the business binding stable during the short Manager commit; network work already finished.
        with closing(self.connect()) as db,db:
            db.execute('BEGIN IMMEDIATE');self._sync(db,session)
            response=self.manager.complete(session,step_id,token,result)
            self._project(db,self.manager.read(session))
            return response

    def sessions(self):
        with closing(self.connect()) as db:
            return [x[0] for x in db.execute('SELECT session_id FROM coach_workspaces ORDER BY goal_id')]

    def process_events(self):
        from services.oj_worker.editor_runs import RunRepository
        runs=RunRepository(self.path);results=OJResultRepository(self.path)
        stats={'finalized':0,'followUps':0}
        with closing(self.connect()) as db:
            ac=db.execute('''SELECT o.oj_result_id,o.goal_id,o.submission_id FROM oj_results o
                LEFT JOIN coach_final_receipts c ON c.result_id=o.oj_result_id
                WHERE o.verdict='AC' AND c.result_id IS NULL LIMIT 20''').fetchall()
        for row in ac:
            contract=self.contracts.ensure_minimal(row['goal_id'])
            report=self.final.verify({'schemaVersion':'1.0.0','requestId':'auto:'+row['oj_result_id'],
                'goalId':row['goal_id'],'goalContractVersion':contract['version'],'submissionId':row['submission_id']})
            with closing(self.connect()) as db,db:
                db.execute('INSERT OR IGNORE INTO coach_final_receipts VALUES(?,?,?)',
                    (row['oj_result_id'],row['goal_id'],json.dumps(report,ensure_ascii=False)))
            stats['finalized']+=1
        with closing(self.connect()) as db:
            links=db.execute('SELECT * FROM coach_execution_links WHERE processed=0 LIMIT 50').fetchall()
        for link in links:
            kind=link['kind'];identifier=link['execution_id']
            if kind=='run':
                task=runs.get(identifier)
                if task['status'] in ('queued','running'):continue
                outcome=task.get('result') or {};cases=outcome.get('cases',[])
                evidence={'id':identifier,'evidenceType':'publicSampleRunResult' if task['runMode']=='publicExample' else 'runResult',
                    'completed':task['status']=='finished','caseResults':cases,'runMode':task['runMode'],
                    'sampleStatus':'passed' if cases and all(c['outcome']=='passed' for c in cases) else 'failed',
                    'status':task['status']}
                evidence['failureCode']=outcome.get('failureCode')
                evidence['failureCategory']='system' if task['status'] in ('failed','interrupted') else None
                if not cases or any(c.get('outcome') not in ('passed','outputMismatch','compileError','runtimeError','timeLimitExceeded','outputLimitExceeded') for c in cases):
                    evidence['sampleStatus']='inconclusive'
                if task['runMode']!='publicExample': evidence.pop('sampleStatus')
            else:
                task=self.editor.submissions.get_submission(identifier)
                if task['status'] in ('awaitingAssets','queued','preparing','running'):continue
                result=results.get_for_submission(identifier)
                if result and result['verdict']=='AC':
                    with closing(self.connect()) as db,db:
                        db.execute('UPDATE coach_execution_links SET processed=1 WHERE kind=? AND execution_id=?',(kind,identifier))
                    continue
                evidence={'id':identifier,'evidenceType':'ojResult','completed':task['status']=='finished',
                    'status':task['status'],'verdict':result['verdict'] if result else None,
                    'testSummary':result['testSummary'] if result else None}
            with closing(self.connect()) as db,db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    self._sync(db,link['session_id'])
                    response=self.manager.command(link['session_id'],'event:'+kind+':'+identifier,'follow_up',
                        {'eventId':identifier,'codeVersion':link['code_version'],'targets':json.loads(link['targets_json']),
                         'evidence':evidence})
                    self._project(db,self.manager.read(link['session_id']))
                    if response['status']!='ignored':stats['followUps']+=1
                except ManagerError as exc:
                    if str(exc)!='COACH_BINDING_CHANGED':raise
                db.execute('UPDATE coach_execution_links SET processed=1 WHERE kind=? AND execution_id=?',(kind,identifier))
        return stats
