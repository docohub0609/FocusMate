import importlib.util
import json
from pathlib import Path

from focusmate.contracts.skill_execution import ProviderResponse
from focusmate.core.context_builder import hash_content


ROOT = Path(__file__).resolve().parents[2]


def load_cli():
    spec = importlib.util.spec_from_file_location('recognize_stage_cli', ROOT / 'tools/recognize-stage.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def input_value():
    goal = {'goalId': 'g', 'subgoalId': 's', 'scope': 'subgoal'}
    values = [('goal', 'goal', {'text': 'finish'}), ('problem', 'problemFact', {'text': 'return x'}),
              ('code', 'code', {'snapshotRef': 'code', 'language': 'python3', 'text': 'x = 1\n'})]
    records = []
    for ref, kind, content in values:
        records.append({'ref': ref, 'goalContext': goal, 'kind': kind, 'version': 1,
                        'contentHash': hash_content(content), 'content': content})
    request = {'schemaVersion': '1.0.0', 'goalContext': goal, 'goalRef': 'goal',
        'problemRequirementRef': 'problem', 'codeSnapshotRef': 'code', 'observationRefs': [],
        'userStatementRefs': [], 'runRecordRefs': [], 'dialogueRefs': [],
        'currentVersions': {'goal': 1, 'problem': 1, 'code': 1},
        'budget': {'contextWindowTokens': 8000, 'reservedOutputTokens': 1000, 'safetyMarginTokens': 500}}
    return {'request': request, 'records': records}


class Provider:
    public_config = {'provider': 'Fake', 'model': 'fake', 'apiKey': 'MUST_NOT_PERSIST'}
    def complete(self, **kwargs):
        value = {'stage': 'implementation_in_progress',
                 'reasons': [{'text': '已有一条完整赋值。', 'evidenceRefs': ['code']}],
                 'unknowns': [{'text': '整体要求尚不清楚。', 'evidenceRefs': ['problem']}]}
        return ProviderResponse(json.dumps(value, ensure_ascii=False), 'fake')


def test_cli_writes_auditable_new_directory_without_secret(tmp_path):
    module = load_cli(); source = tmp_path / 'input.json'
    source.write_text(json.dumps(input_value(), ensure_ascii=False), encoding='utf-8')
    output = tmp_path / 'out'
    assert module.main([str(source), '--output-dir', str(output)], provider_factory=Provider) == 0
    report = json.loads((output / 'report.json').read_text(encoding='utf-8'))
    assert report['status'] == 'recognized'
    assert json.loads((output / 'input.json').read_text(encoding='utf-8')) == input_value()
    config = json.loads((output / 'config.json').read_text(encoding='utf-8'))
    assert config == {'provider': 'Fake', 'model': 'fake'}
    assert (output / 'context.json').is_file() and (output / 'request.json').is_file()
    assert module.main([str(source), '--output-dir', str(output)], provider_factory=Provider) == 1


def test_cli_invalid_input_never_initializes_provider(tmp_path):
    module = load_cli(); source = tmp_path / 'input.json'; source.write_text('{}', encoding='utf-8')
    class MustNotStart:
        def __init__(self): raise AssertionError('provider initialized')
    output = tmp_path / 'out'
    assert module.main([str(source), '--output-dir', str(output)], provider_factory=MustNotStart) == 1
    assert json.loads((output / 'report.json').read_text())['failure']['stage'] == 'input'
