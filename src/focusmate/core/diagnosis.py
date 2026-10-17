"""Pinned diagnostic evidence and structural checks; never learning verification."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
from focusmate.contracts.schema_validation import validate
from focusmate.contracts.manager import safe_payload
from focusmate.core.context_builder import hash_content

PROMPT_VERSION = 't071-diagnosis-v3'


def prepare_diagnosis(request, resolver, *, _check_history=True):
    try:
        validate('DiagnosisRequest', request)
        safe_payload(request)
        refs=[s['ref'] for s in request['sources']]
        required=[request['contractRef'],request['userRequestRef'],*request['baselineRefs'],
                  *request['dialogueRefs'],*request['historyDiagnosisRefs']]
        required += [request[k] for k in ('currentCodeRef','previousActionRef') if k in request]
        if len(set(refs))!=len(refs) or not set(required)<=set(refs):raise ValueError('INVALID_REFERENCES')
        records=[]
        for source in request['sources']:
            try:r=resolver.resolve(source['ref'])
            except Exception:raise ValueError('SOURCE_UNAVAILABLE') from None
            safe_payload(r)
            if r.get('goalContext')!=request['goalContext']:raise ValueError('SOURCE_SCOPE_MISMATCH')
            if any(r.get(k)!=source[k] for k in ('ref','kind','version','contentHash')):raise ValueError('SOURCE_PIN_MISMATCH')
            if not isinstance(r.get('content'),dict) or hash_content(r['content'])!=source['contentHash']:raise ValueError('SOURCE_HASH_MISMATCH')
            for k in ('problemRef','contractVersion','sessionRef'):
                if r.get(k) is not None and request.get(k) is not None and r[k]!=request[k]:raise ValueError('SOURCE_ASSOCIATION_MISMATCH')
            records.append(deepcopy(r))
        by={r['ref']:r for r in records};contract=by[request['contractRef']]['content']
        validate('GoalContract',contract)
        if (by[request['contractRef']]['kind']!='goal' or contract['goalId']!=request['goalContext']['goalId'] or
            contract['version']!=request['contractVersion'] or contract['problemRef']!=request['problemRef'] or
            contract['contractStatus'] not in ('frozen','superseded')):raise ValueError('CONTRACT_MISMATCH')
        ids=[s['subgoalId'] for s in contract['subgoals']]
        if len(set(ids))!=len(ids):raise ValueError('DUPLICATE_SUBGOAL')
        if request['goalContext'].get('subgoalId') and request['goalContext']['subgoalId'] not in ids:raise ValueError('SUBGOAL_NOT_FOUND')
        current=by.get(request.get('currentCodeRef'));missing=[]
        if not current:missing.append('CURRENT_CODE_NOT_PROVIDED')
        elif current['kind']!='code':raise ValueError('CURRENT_CODE_REQUIRED')
        for r in records:
            c=r['content'];kind=r['kind']
            if kind=='code' and (c.get('snapshotRef')!=r['ref'] or c.get('language')!='python3' or not isinstance(c.get('text'),str)):
                raise ValueError('INVALID_CODE_SOURCE')
            link=c.get('codeSnapshotRef')
            if link is not None and (link not in by or by[link]['kind']!='code'):raise ValueError('CODE_REFERENCE_MISSING')
            if kind=='runEvidence' and (not link or c.get('evidenceType') not in ('runResult','compileResult','publicSampleRunResult','targetedRunResult','ojResult')):
                raise ValueError('INVALID_RUN_EVIDENCE')
            if kind=='observation':
                validate('ObservationV2',c.get('observation'))
                if c['observation']['goalContext']!=request['goalContext']:raise ValueError('OBSERVATION_SCOPE_MISMATCH')
                scope=c['observation']['organization']['scope']
                for k in ('problemRef','contractVersion','sessionRef'):
                    if scope.get(k) is not None and request.get(k) is not None and scope[k]!=request[k]:raise ValueError('OBSERVATION_SCOPE_MISMATCH')
            if kind=='dialogue' and (c.get('ref')!=r['ref'] or c.get('role') not in ('user','assistant') or not isinstance(c.get('text'),str)):
                raise ValueError('INVALID_DIALOGUE_SOURCE')
        user=by[request['userRequestRef']]
        if user['kind']!='dialogue' or user['content']['role']!='user':raise ValueError('USER_REQUEST_REQUIRED')
        seq=-1
        for ref in request['dialogueRefs']:
            r=by[ref]
            if r['kind']!='dialogue' or type(r.get('sequence'))!=int or r['sequence']<=seq or not r.get('turnId'):raise ValueError('INVALID_HISTORY_ORDER')
            seq=r['sequence']
        for ref in request['baselineRefs']:
            r=by[ref]
            if ref==request.get('currentCodeRef') or r['kind']=='code' and current and r['version']>=current['version']:raise ValueError('INVALID_BASELINE')
        for ref in request['historyDiagnosisRefs']:
            old=by[ref]['content'].get('diagnosisReport');validate('DiagnosisReport',old)
            if old['goalContext']!=request['goalContext'] or old['problemRef']!=request['problemRef'] or old['contractVersion']!=request['contractVersion']:
                raise ValueError('HISTORY_SCOPE_MISMATCH')
            if _check_history:_validate_history_snapshot(old,request)
        for r in records:
            if r['kind']!='hypothesis' or r['ref'] in request['historyDiagnosisRefs']:continue
            c=r['content']
            if r['ref']==request.get('previousActionRef'):
                action=c.get('action');validate({'2.0.0':'LearningActionV2','3.0.0':'LearningActionV3'}.get(action.get('schemaVersion') if isinstance(action,dict) else None,'LearningAction'),action)
                if action['goalContext']!=request['goalContext']:raise ValueError('ACTION_SCOPE_MISMATCH')
                continue
            opinion=c.get('stageReport') or c.get('progressReport')
            if not isinstance(opinion,dict):raise ValueError('UNSUPPORTED_CACHED_OPINION')
            title=('StageRecognitionReportV2' if opinion.get('schemaVersion')=='2.0.0' else 'StageRecognitionReport') if 'stageReport' in c else 'ProgressEstimationReport'
            validate(title,opinion)
            if opinion['goalContext']!=request['goalContext'] or opinion['status'] not in ('recognized','estimated'):raise ValueError('INVALID_CACHED_OPINION')
            if 'stageReport' in c:
                pin=opinion['codeSnapshot']
                if pin['ref'] not in by or any(pin[k]!=by[pin['ref']][k] for k in ('version','contentHash')):raise ValueError('OPINION_CODE_MISMATCH')
            else:
                snap=opinion.get('materialSnapshot',{})
                # A progress opinion without its original input cannot be safely reused.
                if not snap:raise ValueError('OPINION_SCOPE_MISSING')
                oldreq=snap.get('request',{})
                if any(oldreq.get(k)!=request.get(k) for k in ('goalContext','problemRef','contractVersion','currentCodeRef','baselineRefs')):
                    raise ValueError('OPINION_SCOPE_MISMATCH')
                for source in oldreq.get('sources',[]):
                    if source['ref'] not in by or any(source[k]!=by[source['ref']][k] for k in ('version','contentHash')):raise ValueError('OPINION_SOURCE_MISMATCH')
        return {'request':deepcopy(request),'records':records,'missing':missing}
    except ValueError:raise
    except Exception:raise ValueError('INVALID_DIAGNOSIS_INPUT') from None


def _validate_history_snapshot(report, current_request):
    """Check each saved input against its own pins, never rebind old code to new."""
    from focusmate.core.context_builder import MapContextResolver
    pending=[report];seen=set()
    while pending:
        old=pending.pop();key=hash_content(old)
        if key in seen:continue
        seen.add(key);validate('DiagnosisReport',old)
        snap=old.get('inputSnapshot',{})
        if not isinstance(snap.get('request'),dict) or not isinstance(snap.get('records'),list):
            raise ValueError('HISTORY_SNAPSHOT_MISSING')
        oldreq=snap['request']
        for name in ('goalContext','problemRef','contractVersion'):
            if old.get(name)!=oldreq.get(name) or oldreq.get(name)!=current_request.get(name):
                raise ValueError('HISTORY_SCOPE_MISMATCH')
        if oldreq.get('sessionRef') and current_request.get('sessionRef') and oldreq['sessionRef']!=current_request['sessionRef']:
            raise ValueError('HISTORY_SESSION_MISMATCH')
        if len({r['ref'] for r in snap['records']})!=len(snap['records']):raise ValueError('HISTORY_DUPLICATE_SOURCE')
        bundle=prepare_diagnosis(oldreq,MapContextResolver(snap['records']),_check_history=False)
        extras=snap.get('actualMaterials',[])
        refs={r['ref'] for r in bundle['records']}
        for r in extras:
            if r['ref'] in refs or r.get('goalContext')!=oldreq['goalContext'] or hash_content(r.get('content'))!=r.get('contentHash'):
                raise ValueError('HISTORY_SUPPLEMENT_MISMATCH')
            refs.add(r['ref'])
        validate_diagnosis({k:old[k] for k in ('questions','contractConcerns')},bundle,additional_records=extras)
        by={r['ref']:r for r in bundle['records']}
        pending.extend(by[ref]['content']['diagnosisReport'] for ref in oldreq['historyDiagnosisRefs'])


def validate_diagnosis(value, bundle, *, additional_records=(), runtime_goals=None):
    try:validate('DiagnosisCandidates',value)
    except Exception:raise ValueError('INVALID_DIAGNOSIS_FORMAT') from None
    request=bundle['request'];records=bundle['records']+list(additional_records)
    by={r['ref']:r for r in records};contract=by[request['contractRef']]['content']
    subgoals={s['subgoalId'] for s in (runtime_goals if runtime_goals is not None else contract['subgoals'])}
    previous={ref+':'+q['questionKey'] for ref in request['historyDiagnosisRefs']
              for q in by[ref]['content']['diagnosisReport']['questions']}
    keys=[q['questionKey'] for q in value['questions']]
    if len(keys)!=len(set(keys)):raise ValueError('DUPLICATE_QUESTION')
    allrefs=[];graph={}
    def check(item):
        if not set(item['evidenceRefs'])<=by.keys():raise ValueError('INVALID_EVIDENCE_REFERENCE')
        allrefs.extend(item['evidenceRefs'])
    for q in value['questions']:
        check(q);check(q['history']);graph[q['questionKey']]=q['dependsOn']
        if not set(q['dependsOn'])<=set(keys):raise ValueError('UNKNOWN_QUESTION_DEPENDENCY')
        h=q['history']
        if not set(h['previousQuestionRefs'])<=previous or h['relation']=='same' and not h['previousQuestionRefs']:
            raise ValueError('INVALID_HISTORY_RELATION')
        for s in q['subgoals']:
            if s['subgoalId'] not in subgoals:raise ValueError('UNKNOWN_SUBGOAL')
            check(s)
        for item in q['findings']+q['unknowns']:check(item)
        if not q['findings'] and not q['unknowns']:raise ValueError('EMPTY_DIAGNOSIS')
    def visit(key,stack):
        if key in stack:raise ValueError('QUESTION_DEPENDENCY_CYCLE')
        for child in graph[key]:visit(child,stack|{key})
    for key in graph:visit(key,set())
    for c in value['contractConcerns']:check(c)
    report={'schemaVersion':'1.0.0','diagnosisId':'diagnosis-'+uuid4().hex,
            'createdAt':datetime.now(timezone.utc).isoformat(),'goalContext':deepcopy(request['goalContext']),
            'problemRef':deepcopy(request['problemRef']),'contractVersion':request['contractVersion'],
            'status':'diagnosed','handoff':'action_planning_not_integrated','semanticStatus':'notVerified',
            **deepcopy(value),'evidenceRefs':list(dict.fromkeys(allrefs)),
            'inputSnapshot':deepcopy(bundle),'promptVersion':PROMPT_VERSION}
    validate('DiagnosisReport',report)
    return report
