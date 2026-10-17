"""The caller chooses a task; source validation is never delegated to a model."""
from copy import deepcopy
import json

import pytest

from focusmate.contracts.skill_execution import ProviderError, ProviderResponse
from focusmate.core.context_builder import MapContextResolver
from focusmate.core.stage_recognition import recognize_stage
from tools.stage_local_demo import create_case_runner
from tools.stage_local_evaluation import load_cases


class Stage:
    public_config = {'model': 'fake', 'maxTokens': 512}
    def __init__(self, error=False):
        self.calls = []; self.error = error
    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error: raise ProviderError('PROVIDER_UNAVAILABLE')
        return ProviderResponse(json.dumps({'stage': 'unknown', 'reasons': [], 'unknowns': [
            {'text': '缺少整体要求。', 'evidenceRefs': ['p01']}]}), 'fake')


class Dialogue:
    public_config = {'model': 'fake', 'maxTokens': 512}
    def __init__(self): self.calls = []
    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return {'content': '当前代码只完成初始化。', 'toolCalls': [], 'model': 'fake'}


def supplied(mode):
    value = deepcopy(load_cases()[0]['input']); value['mode'] = mode
    return value


def test_stage_v2_does_not_ask_model_for_execution_advice():
    value = supplied('stage_only'); provider = Stage()
    report = recognize_stage(value['stageRequest'], MapContextResolver(value['records']), provider)
    assert report['status'] == 'recognized'
    assert report['schemaVersion'] == '2.0.0'
    assert set(report['result']) == {'stage', 'reasons', 'unknowns'}
    payload = json.loads(provider.calls[0]['user_prompt'])
    assert 'localTestableScopes' not in payload['materials']['outputSchema']['properties']


def test_invalid_goal_has_a_renderable_failure_without_inventing_ownership():
    from focusmate.core.stage_recognition import render_stage_report
    value = supplied('stage_only'); value['stageRequest']['goalContext'] = {}
    provider = Stage()
    report = recognize_stage(value['stageRequest'], MapContextResolver(value['records']), provider)
    assert report['status'] == 'failed' and not provider.calls
    assert report['goalContext'] == {'goalId': 'invalid', 'scope': 'goal'}
    assert 'INVALID_INPUT' in render_stage_report(report)


def test_stage_only_never_starts_dialogue_or_database(tmp_path):
    first, second = Stage(), Dialogue()
    report = create_case_runner(first, second)(supplied('stage_only'), tmp_path)
    assert report['status'] == 'completed'
    assert len(first.calls) == 1 and not second.calls
    assert not list(tmp_path.glob('*.sqlite3'))
    assert '材料不足' in report['display']
    assert report['dialogueReport'] is None


def test_assist_only_has_full_materials_and_no_stage_call(tmp_path):
    first, second = Stage(), Dialogue(); value = supplied('assist_only')
    current = next(r for r in value['records'] if r['kind'] == 'code')
    value['records'] = list(reversed(value['records']))
    report = create_case_runner(first, second)(value, tmp_path)
    assert report['status'] == 'completed' and not first.calls
    payload = json.loads(second.calls[0]['messages'][1]['content'])
    assert payload['materials']['goal']['requirement']
    assert payload['materials']['code'] == current['content']
    assert 'observations' in payload['materials']
    assert 'recognizedStage' not in payload['materials']


@pytest.mark.parametrize('mode', ['stage_only', 'assist_only', 'combined'])
def test_bad_shared_hash_stops_every_path(mode, tmp_path):
    value = supplied(mode); value['records'][0]['contentHash'] = 'sha256:' + '0' * 64
    first, second = Stage(), Dialogue()
    report = create_case_runner(first, second)(value, tmp_path)
    assert report['status'] == 'input_failed'
    assert not first.calls and not second.calls


@pytest.mark.parametrize('mode', ['stage_only', 'assist_only', 'combined'])
@pytest.mark.parametrize('fault', ['dialogue_ref', 'dialogue_text', 'boolean_version',
                                 'sequence_missing', 'sequence_boolean', 'turn_missing'])
def test_shared_validation_rejects_malformed_source_before_either_model(mode, fault, tmp_path):
    from focusmate.core.context_builder import hash_content
    value = supplied(mode)
    record = next(r for r in value['records'] if r['kind'] == 'dialogue')
    if fault == 'dialogue_ref': record['content']['ref'] = 'other-user-statement'
    elif fault == 'dialogue_text': record['content']['text'] = {'text': 'nested'}
    elif fault == 'boolean_version': record['version'] = True
    elif fault == 'sequence_missing': record.pop('sequence')
    elif fault == 'sequence_boolean': record['sequence'] = True
    elif fault == 'turn_missing': record.pop('turnId')
    record['contentHash'] = hash_content(record['content'])
    first, second = Stage(), Dialogue()
    report = create_case_runner(first, second)(value, tmp_path)
    assert report['status'] == 'input_failed'
    assert not first.calls and not second.calls
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('mode', ['stage_only', 'assist_only', 'combined'])
@pytest.mark.parametrize('fault', ['sequence_reversed', 'turn_reopened'])
def test_shared_validation_rejects_invalid_history_order(mode, fault, tmp_path):
    from focusmate.core.context_builder import hash_content
    value = supplied(mode); request = value['stageRequest']
    request['userStatementRefs'] = list(request['userStatementRefs'])
    request['dialogueRefs'] = list(request['dialogueRefs'])
    first_record = next(r for r in value['records'] if r['kind'] == 'dialogue')
    for index in (2, 3):
        record = deepcopy(first_record); record.update(ref=f'u{index:02}', sequence=index,
            turnId='middle' if index == 2 else first_record['turnId'])
        record['content']['ref'] = record['ref']
        record['contentHash'] = hash_content(record['content'])
        value['records'].append(record)
        request['userStatementRefs'].append(record['ref'])
        request['dialogueRefs'].append(record['ref'])
        request['currentVersions'][record['ref']] = record['version']
    if fault == 'sequence_reversed': request['dialogueRefs'].reverse()
    first, second = Stage(), Dialogue()
    report = create_case_runner(first, second)(value, tmp_path)
    assert report['status'] == 'input_failed'
    assert report['failure']['reason'] == ('INVALID_MESSAGE_ORDER' if fault == 'sequence_reversed'
                                          else 'INVALID_TURN_GROUP')
    assert not first.calls and not second.calls


def test_stage_service_failure_does_not_cancel_independent_help(tmp_path):
    first, second = Stage(error=True), Dialogue()
    report = create_case_runner(first, second)(supplied('combined'), tmp_path)
    assert report['status'] == 'partial'
    assert report['stageReport']['status'] == 'failed'
    assert report['dialogueReport']['status'] == 'answered'
    payload = json.loads(second.calls[0]['messages'][1]['content'])
    assert 'recognizedStage' not in payload['materials']


def test_invalid_mode_stops_before_models(tmp_path):
    first, second = Stage(), Dialogue()
    report = create_case_runner(first, second)(supplied('guess'), tmp_path)
    assert report['status'] == 'input_failed'
    assert not first.calls and not second.calls


def test_old_report_is_read_without_a_model():
    from pathlib import Path
    from focusmate.core.stage_recognition import render_stage_report
    path = Path('tests/fixtures/legacy-stage-report.json')
    report = json.loads(path.read_text(encoding='utf-8'))
    before = deepcopy(report)
    assert report['schemaVersion'] == '1.0.0'
    assert '暂不能判断' in render_stage_report(report)
    assert report == before


def test_assist_budget_failure_calls_neither_model(tmp_path):
    value = supplied('assist_only'); value['stageRequest']['budget']['contextWindowTokens'] = 100
    first, second = Stage(), Dialogue()
    report = create_case_runner(first, second)(value, tmp_path)
    assert report['dialogueReport']['failure']['code'] == 'CONTEXT_BUILD_FAILED'
    assert not first.calls and not second.calls


def test_combined_unknown_is_valid_and_uses_same_evidence(tmp_path):
    first, second = Stage(), Dialogue()
    report = create_case_runner(first, second)(supplied('combined'), tmp_path)
    assert report['status'] == 'completed'
    progress = json.loads(first.calls[0]['user_prompt'])['materials']
    help_materials = json.loads(second.calls[0]['messages'][1]['content'])['materials']
    for key in ('goal', 'problemRequirement', 'code', 'observations', 'runRecords', 'historicalCodes'):
        assert progress[key] == help_materials[key]
    assert help_materials['progressOpinion']['stage'] == 'unknown'


@pytest.mark.parametrize('field', ['codeVersion', 'goalContext'])
def test_outer_context_cannot_replace_validated_sources(field, tmp_path):
    value = supplied('assist_only')
    value[field] = 987 if field == 'codeVersion' else {'goalId': 'another', 'scope': 'goal'}
    first, second = Stage(), Dialogue()
    assert create_case_runner(first, second)(value, tmp_path)['status'] == 'input_failed'
    assert not first.calls and not second.calls


def test_unknown_has_missing_facts_instead_of_another_stage_justification():
    class Contradiction(Stage):
        def complete(self, **kwargs):
            return ProviderResponse(json.dumps({'stage': 'unknown', 'reasons': [
                {'text': '核心算法没写。', 'evidenceRefs': ['c01']}],
                'unknowns': [{'text': '没有题目要求。', 'evidenceRefs': ['p01']}]}), 'fake')
    value = supplied('stage_only')
    report = recognize_stage(value['stageRequest'], MapContextResolver(value['records']), Contradiction())
    assert report['status'] == 'failed'
    assert report['failure']['reason'] == 'PROVIDER_OUTPUT_INVALID'
