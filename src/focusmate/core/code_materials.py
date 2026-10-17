"""Read caller-pinned code evidence for either progress or assistance, without model calls."""
from copy import deepcopy
from focusmate.contracts.schema_validation import validate
from focusmate.core.context_builder import hash_content


class CodeMaterialError(Exception):
    def __init__(self, reason): self.reason = reason


def material_refs(request):
    return [request['goalRef'], request['problemRequirementRef'], request['codeSnapshotRef'],
            *request['observationRefs'], *request['userStatementRefs'], *request['runRecordRefs'],
            *request.get('historicalCodeRefs', [])]


def _reject_sensitive(value):
    forbidden = {'hiddentests', 'hidden_tests', 'oracle', 'apikey', 'api_key', 'secret', 'password', 'token'}
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = ''.join(ch.lower() for ch in str(key) if ch.isalnum() or ch == '_')
            if normalized in forbidden: raise CodeMaterialError('FORBIDDEN_INPUT')
            _reject_sensitive(nested)
    elif isinstance(value, list):
        for nested in value: _reject_sensitive(nested)


def resolve_code_materials(request, resolver):
    validate('StageRecognitionRequest', request)
    expected = {request['goalRef']: 'goal', request['problemRequirementRef']: 'problemFact',
                request['codeSnapshotRef']: 'code'}
    expected.update({ref: 'observation' for ref in request['observationRefs']})
    expected.update({ref: 'dialogue' for ref in request['userStatementRefs']})
    expected.update({ref: 'runEvidence' for ref in request['runRecordRefs']})
    expected.update({ref: 'code' for ref in request.get('historicalCodeRefs', [])})
    refs = material_refs(request)
    if len(refs) != len(set(refs)) or set(request['currentVersions']) != set(refs):
        raise CodeMaterialError('INVALID_REFERENCES')
    if not set(request['dialogueRefs']) <= set(request['userStatementRefs']):
        raise CodeMaterialError('INVALID_REFERENCES')
    records = []
    for ref in refs:
        try: record = resolver.resolve(ref)
        except (KeyError, ValueError): raise CodeMaterialError('SOURCE_NOT_FOUND') from None
        if record.get('ref') != ref: raise CodeMaterialError('SOURCE_REFERENCE_MISMATCH')
        if record.get('goalContext') != request['goalContext']: raise CodeMaterialError('SOURCE_SCOPE_MISMATCH')
        if record.get('kind') != expected[ref]: raise CodeMaterialError('SOURCE_KIND_MISMATCH')
        if type(record.get('version')) not in (str, int) or not record.get('version'):
            raise CodeMaterialError('SOURCE_VERSION_MISSING')
        if record.get('version') != request['currentVersions'][ref]: raise CodeMaterialError('SOURCE_VERSION_MISMATCH')
        if 'content' not in record or record.get('contentHash') != hash_content(record['content']):
            raise CodeMaterialError('SOURCE_HASH_MISMATCH')
        if not isinstance(record['content'], dict):
            raise CodeMaterialError('SOURCE_CONTENT_INVALID')
        _reject_sensitive(record['content'])
        if record['kind'] == 'dialogue' and (
                record['content'].get('ref') != ref or
                not isinstance(record['content'].get('text'), str)):
            raise CodeMaterialError('SOURCE_REFERENCE_MISMATCH')
        records.append(record)
    # These checks must run even when progress/context summarization is skipped.
    by_ref = {record['ref']: record for record in records}
    previous_sequence, previous_turn, closed_turns = -1, None, set()
    for ref in request['dialogueRefs']:
        record = by_ref[ref]
        sequence, turn = record.get('sequence'), record.get('turnId')
        if (type(sequence) is not int or sequence <= previous_sequence or
                not isinstance(turn, str) or not turn):
            raise CodeMaterialError('INVALID_MESSAGE_ORDER')
        if turn != previous_turn:
            if turn in closed_turns:
                raise CodeMaterialError('INVALID_TURN_GROUP')
            if previous_turn is not None:
                closed_turns.add(previous_turn)
        previous_sequence, previous_turn = sequence, turn
    code_refs = {r['ref'] for r in records if r['kind'] == 'code'}
    for record in records:
        linked = record['content'].get('codeSnapshotRef') if isinstance(record['content'], dict) else None
        if record['kind'] in ('observation', 'runEvidence') and linked not in code_refs:
            raise CodeMaterialError('CODE_SNAPSHOT_REFERENCE_MISSING')
        if record['kind'] == 'code' and record['content'].get('snapshotRef') != record['ref']:
            raise CodeMaterialError('SOURCE_REFERENCE_MISMATCH')
        if record['kind'] == 'code' and (not isinstance(record['content'].get('text'), str)
                                        or not isinstance(record['content'].get('language'), str)):
            raise CodeMaterialError('SOURCE_CONTENT_INVALID')
    return records


def code_material_payload(request, records):
    by_ref = {r['ref']: r for r in records}
    current = request['codeSnapshotRef']
    def linked(ref):
        return {**deepcopy(by_ref[ref]['content']), 'ref': ref,
                'isCurrentCode': by_ref[ref]['content'].get('codeSnapshotRef') == current}
    code = deepcopy(by_ref[current]['content'])
    return {
        'goal': deepcopy(by_ref[request['goalRef']]['content']),
        'problemRequirement': deepcopy(by_ref[request['problemRequirementRef']]['content']),
        'code': code,
        'codeLines': [{'line': i, 'text': line} for i, line in enumerate(code['text'].splitlines(), 1)],
        'observations': [linked(ref) for ref in request['observationRefs']],
        'userStatements': [deepcopy(by_ref[ref]['content']) for ref in request['userStatementRefs']],
        'runRecords': [linked(ref) for ref in request['runRecordRefs']],
        'historicalCodes': [deepcopy(by_ref[ref]) for ref in request.get('historicalCodeRefs', [])],
        'evidenceCatalog': [{'ref': r['ref'], 'kind': r['kind'], 'version': r['version'],
                             'contentHash': r['contentHash']} for r in records]}
