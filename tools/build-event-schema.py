"""Rebuild the explicit v2 event schema without changing the historical v1 contract."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build():
    original = json.loads((ROOT / 'schemas/process/raw-event.schema.json').read_text(encoding='utf-8'))
    schema = copy.deepcopy(original)
    schema.update(title='RawEventV2')
    schema['$id'] = 'https://focusmate.local/schemas/process/raw-event-v2.schema.json'
    schema.pop('examples')
    props = schema['properties']
    props['schemaVersion'] = {'const': '2.0.0'}
    props['eventVersion'] = {'const': 2}
    props['source'] = {'enum': ['api', 'ojWorker', 'toolExecutor', 'developerFixture']}
    props['actor'] = {'enum': ['user', 'model', 'system', 'simulated']}
    props['sessionRef'] = {'type': 'string', 'minLength': 1, 'maxLength': 256}
    props['contractVersion'] = {'type': 'integer', 'minimum': 1}
    schema['required'].append('actor')
    text = {'type': 'string', 'minLength': 1, 'maxLength': 256}
    problem = {'$ref': 'https://focusmate.local/schemas/common/definitions.schema.json#/$defs/problemRef'}
    for variant in schema['oneOf']:
        p = variant['properties']; name = p['eventName']['const']
        p['source'] = {'const': 'ojWorker' if name == 'ojResultProduced' else 'api'}
        if name in ('helpRequested', 'actionFeedbackProvided', 'sessionPaused', 'sessionResumed', 'runRequested'):
            p['source'] = {'enum': ['api', 'developerFixture']}
        if name in ('codeSnapshotCreated', 'runRequested'):
            p['goalContext'] = copy.deepcopy(props['goalContext'])
        payload = p['payload']; payload.setdefault('required', [])
        if name == 'codeSnapshotCreated':
            payload['required'].remove('draftState'); del payload['properties']['draftState']
        if name in ('codeSnapshotCreated', 'runRequested', 'ojResultProduced'):
            payload['required'].append('problemRef'); payload['properties']['problemRef'] = problem
        if name == 'submissionRequested':
            payload['required'].append('submissionId'); payload['properties']['submissionId'] = text
        if name == 'runRequested':
            payload['properties']['runId'] = text
        if name == 'ojResultProduced':
            payload['required'].append('codeVersion'); payload['properties']['codeVersion'] = {'type': 'integer', 'minimum': 1}
        if name in ('sessionPaused', 'sessionResumed'):
            variant['required'] = ['sessionRef']
    props['eventName']['enum'].append('publicRunFinished')
    schema['oneOf'].append({'properties': {
        'eventName': {'const': 'publicRunFinished'}, 'source': {'const': 'toolExecutor'},
        'privacyLevel': {'const': 'taskContent'},
        'payload': {'type': 'object', 'additionalProperties': False,
            'required': ['runId', 'loopId', 'codeVersion', 'problemRef', 'status'],
            'properties': {'runId': text, 'loopId': text,
                'codeVersion': {'type': 'integer', 'minimum': 1}, 'problemRef': problem,
                'status': {'enum': ['succeeded', 'failed', 'cancelled', 'interrupted']},
                'sampleStatus': {'enum': ['passed', 'failed', 'infrastructureError']}}}}})
    schema['allOf'] = [{'if': {'properties': {'goalContext': {'properties': {'scope': {'const': 'subgoal'}}}}},
                        'then': {'required': ['contractVersion']}},
                       {'if': {'properties': {'source': {'const': 'developerFixture'}}},
                        'then': {'properties': {'actor': {'const': 'simulated'}}}}]
    (ROOT / 'schemas/process/raw-event-v2.schema.json').write_text(json.dumps(schema, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    build()
