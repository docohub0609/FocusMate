"""Validate pinned evidence for progress estimation; model opinions never become facts."""
from copy import deepcopy
from focusmate.contracts.schema_validation import validate
from focusmate.contracts.progress import ProgressSourceError
from focusmate.core.context_builder import hash_content
from focusmate.core.code_materials import _reject_sensitive


class ProgressInputError(ValueError):
    pass


def _private_fields(value):
    if isinstance(value,dict):
        if set(value)&{'backendLogs','internalLogs','hiddenTests','oracle','credentials','authorization'}:
            raise ProgressInputError('SENSITIVE_MATERIAL')
        for child in value.values():_private_fields(child)
    elif isinstance(value,list):
        for child in value:_private_fields(child)


def prepare_progress_materials(request, resolver):
    validate('ProgressEstimationRequest', request)
    refs = [s['ref'] for s in request['sources']]
    if len(refs) != len(set(refs)) or not set([request['contractRef'], request['currentCodeRef'], *request['baselineRefs']]) <= set(refs):
        raise ProgressInputError('INVALID_REFERENCES')
    if request['currentCodeRef'] in request['baselineRefs'] or request['contractRef'] in request['baselineRefs']:
        raise ProgressInputError('INVALID_BASELINE')
    records=[]; last_sequence=-1; last_turn=None; closed=set(); missing=[]
    for source in request['sources']:
        try: r=resolver.resolve(source['ref'])
        except ProgressSourceError as error:raise ProgressInputError(error.code) from None
        except (KeyError, ValueError): raise ProgressInputError('SOURCE_UNAVAILABLE') from None
        if r.get('goalContext') != request['goalContext']: raise ProgressInputError('SOURCE_SCOPE_MISMATCH')
        if r.get('ref') != source['ref'] or r.get('kind') != source['kind']: raise ProgressInputError('SOURCE_REFERENCE_MISMATCH')
        if type(r.get('version')) is not int or r['version'] != source['version']: raise ProgressInputError('SOURCE_VERSION_MISMATCH')
        if not isinstance(r.get('content'),dict): raise ProgressInputError('SOURCE_CONTENT_INVALID')
        if r.get('contentHash') != source['contentHash'] or r['contentHash'] != hash_content(r['content']):
            raise ProgressInputError('SOURCE_HASH_MISMATCH')
        # The audit snapshot retains metadata too, so its entire envelope must be safe.
        _reject_sensitive(r)
        _private_fields(r)
        # Explicit association is checked, never silently invented for older sources.
        for field,expected in [('problemRef',request['problemRef']),('contractVersion',request['contractVersion']),('sessionRef',request.get('sessionRef'))]:
            actual=r.get(field)
            if actual is not None and expected is not None and actual != expected:
                raise ProgressInputError('SOURCE_ASSOCIATION_MISMATCH')
        content=r['content']
        if r['kind']=='code':
            if content.get('snapshotRef') != r['ref'] or content.get('language') != 'python3' or not isinstance(content.get('text'),str):
                raise ProgressInputError('INVALID_CODE_SOURCE')
        if r['kind']=='dialogue':
            if content.get('ref') != r['ref'] or not isinstance(content.get('text'),str) or content.get('role') != 'user':
                raise ProgressInputError('INVALID_DIALOGUE_SOURCE')
            seq=r.get('sequence');turn=r.get('turnId')
            if type(seq) is not int or seq<=last_sequence or not isinstance(turn,str) or not turn:
                raise ProgressInputError('INVALID_MESSAGE_ORDER')
            if turn!=last_turn:
                if turn in closed:raise ProgressInputError('INVALID_TURN_GROUP')
                if last_turn is not None:closed.add(last_turn)
            last_sequence,last_turn=seq,turn
        if r['kind']=='observation':
            observation=content.get('observation')
            validate('ObservationV2',observation)
            if observation['goalContext']!=request['goalContext']:raise ProgressInputError('OBSERVATION_SCOPE_MISMATCH')
            scope=observation['organization']['scope']
            for key, expected in [('problemRef',request['problemRef']),('contractVersion',request['contractVersion']),('sessionRef',request.get('sessionRef'))]:
                if scope.get(key) is not None and expected is not None and scope[key]!=expected:raise ProgressInputError('OBSERVATION_SCOPE_MISMATCH')
                if scope.get(key) is None:missing.append('ASSOCIATION_UNSPECIFIED:'+r['ref']+':'+key)
        if r['kind']=='hypothesis':
            stage=content.get('stageReport')
            validate('StageRecognitionReportV2' if isinstance(stage,dict) and stage.get('schemaVersion')=='2.0.0' else 'StageRecognitionReport',stage)
            if stage['goalContext']!=request['goalContext'] or stage['status']!='recognized':raise ProgressInputError('INVALID_STAGE_OPINION')
        records.append(deepcopy(r))
    by_ref={r['ref']:r for r in records}
    contract=by_ref[request['contractRef']]
    if contract['kind']!='goal':raise ProgressInputError('INVALID_CONTRACT_SOURCE')
    contract=contract['content'];validate('GoalContract',contract)
    if (contract['goalId']!=request['goalContext']['goalId'] or contract['version']!=request['contractVersion'] or
        contract['problemRef']!=request['problemRef'] or contract['contractStatus'] not in ('frozen','superseded')):
        raise ProgressInputError('CONTRACT_MISMATCH')
    subgoals={s['subgoalId']:s for s in contract['subgoals']}
    if len(subgoals)!=len(contract['subgoals']):raise ProgressInputError('DUPLICATE_SUBGOAL')
    if request['goalContext']['scope']=='subgoal' and request['goalContext']['subgoalId'] not in subgoals:raise ProgressInputError('SUBGOAL_NOT_FOUND')
    current=by_ref[request['currentCodeRef']]
    if current['kind']!='code':raise ProgressInputError('CURRENT_CODE_REQUIRED')
    for ref in request['baselineRefs']:
        baseline=by_ref[ref]
        if baseline['kind'] in ('goal','problemFact') or (baseline['kind']=='code' and baseline['version']>=current['version']):
            raise ProgressInputError('INVALID_BASELINE')
    code_refs={r['ref'] for r in records if r['kind']=='code'}
    for r in records:
        linked=r['content'].get('codeSnapshotRef')
        if linked is not None and linked not in code_refs:raise ProgressInputError('CODE_REFERENCE_MISSING')
        if r['kind']=='runEvidence':
            if linked not in code_refs:raise ProgressInputError('CODE_REFERENCE_MISSING')
            if r['content'].get('evidenceType') not in ('runResult','compileResult','publicSampleRunResult','targetedRunResult','ojResult'):
                raise ProgressInputError('INVALID_RUN_EVIDENCE')
        if r['kind']=='hypothesis':
            pin=r['content']['stageReport']['codeSnapshot']
            if pin['ref'] not in code_refs or pin['version']!=by_ref[pin['ref']]['version'] or pin['contentHash']!=by_ref[pin['ref']]['contentHash']:
                raise ProgressInputError('STAGE_CODE_MISMATCH')
    if not request['baselineRefs']:missing.append('BASELINE_NOT_PROVIDED')
    return records,list(dict.fromkeys(missing))


def validate_progress_candidate(value, request, records):
    validate('ProgressEstimationCandidates',value)
    by_ref={r['ref']:r for r in records}; current=request['currentCodeRef']
    contract=by_ref[request['contractRef']]['content'];criteria={s['subgoalId']:s for s in contract['subgoals']}
    seen={}
    for f in value['findings']:
        refs=f['evidenceRefs']
        if not set(refs)<=by_ref.keys():raise ProgressInputError('INVALID_EVIDENCE_REFERENCE')
        if f['kind'] in ('progressing','no_material_change','regression_candidate'):
            if not request['baselineRefs'] or not set(refs)&set(request['baselineRefs']):raise ProgressInputError('COMPARISON_BASELINE_REQUIRED')
            if not set(refs)-set(request['baselineRefs'])-{request['contractRef']}:raise ProgressInputError('CURRENT_COMPARISON_REQUIRED')
        kinds=seen.setdefault(f['subject'].strip(),set());kinds.add(f['kind'])
        if 'no_material_change' in kinds and kinds&{'progressing','regression_candidate'}:raise ProgressInputError('CONTRADICTORY_FINDINGS')
        if f['kind']=='verification_candidate':
            v=f['verification'];target=v['criterionId'];typ=v['evidenceType'];materials=v['materialRefs']
            if not set(materials)<=set(refs):raise ProgressInputError('VERIFICATION_REFERENCE_MISMATCH')
            if target=='finalOj':
                if request['goalContext']['scope']!='goal' or typ!='ojResult':raise ProgressInputError('INVALID_VERIFICATION_TARGET')
            elif target not in criteria or typ not in criteria[target]['acceptableEvidence'] or (request['goalContext']['scope']=='subgoal' and target!=request['goalContext']['subgoalId']):
                raise ProgressInputError('INVALID_VERIFICATION_TARGET')
            for ref in materials:
                r=by_ref[ref];content=r['content'];linked=content.get('codeSnapshotRef')
                if linked is not None and linked!=current:raise ProgressInputError('OLD_VERIFICATION_EVIDENCE')
                valid=(typ=='evaluatedUserExplanation' and r['kind']=='dialogue' or
                    typ in ('complexityCheck','codeStructureCheck') and r['kind']=='code' and ref==current or
                    typ=='confirmedProblemFeature' and r['kind']=='problemFact' and content.get('confirmed') is True or
                    r['kind']=='runEvidence' and content.get('evidenceType')==typ and content.get('completed') is True)
                if not valid:raise ProgressInputError('INCOMPATIBLE_VERIFICATION_MATERIAL')
    return list(dict.fromkeys(ref for f in value['findings'] for ref in f['evidenceRefs']))
