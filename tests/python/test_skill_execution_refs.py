"""Valid offline package references must remain usable during execution."""
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from focusmate.contracts import skill_schemas
from focusmate.contracts.skill_files import fingerprint, read_files
from focusmate.core.skill_execution import _prepare, _candidate
from focusmate.core.skill_package import validate_skill_package


ROOT = Path(__file__).resolve().parents[2]


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def _package(tmp_path, target):
    path = tmp_path / 'tree-and-graph'
    shutil.copytree(ROOT / 'skills/tree-and-graph', path)
    filename = 'input.schema.json' if target == 'input' else 'result.schema.json'
    schema = json.loads((path / 'schemas' / filename).read_text(encoding='utf-8'))
    if target == 'input':
        schema['$defs']['evidence']['properties']['text'] = {'$ref': 'reason.schema.json'}
    else:
        schema['properties']['applicability']['properties']['reason'] = {'$ref': 'reason.schema.json'}
    _write(path / 'schemas' / filename, schema)
    _write(path / 'schemas/reason.schema.json', {
        '$schema': skill_schemas.DIALECT, 'type': 'string', 'minLength': 1,
        'examples': ['private sample'], 'default': 'private default'})
    _write(path / 'schemas/unrelated.schema.json', {
        '$schema': skill_schemas.DIALECT, 'type': 'integer', 'description': 'unrelated secret'})
    descriptor = json.loads((path / 'descriptor.json').read_text(encoding='utf-8'))
    descriptor['hash'] = fingerprint(read_files(path), descriptor)[0]
    _write(path / 'descriptor.json', descriptor)
    assert validate_skill_package(path)['packageValid']
    request = {'goalContext': {'goalId': 'g', 'scope': 'goal'},
        'skillRef': {'skillId': descriptor['skillId'], 'skillVersion': descriptor['version'],
                     'skillHash': descriptor['hash']},
        'inputs': {'userRequest': {'ref': 'req', 'text': '检查遍历'},
                   'goalRequirement': {'ref': 'goal', 'text': '解释局部'}, 'problemFacts': [],
                   'code': {'snapshotRef': 'code', 'text': 'seen.add(u)'}, 'runEvidence': []},
        'hintLevel': 2, 'dialogue': []}
    return path, request


@pytest.mark.parametrize('target', ['input', 'output'])
def test_valid_relative_references_work_during_execution(tmp_path, target):
    _, request = _package(tmp_path, target)
    payload, validator, refs = _prepare(request, tmp_path)
    candidate = {'applicability': {'status': 'applicable', 'reason': '检查遍历',
                                  'evidenceRefs': ['req']}, 'findings': [], 'unknowns': []}
    _candidate(candidate, validator, refs)
    if target == 'output':
        resources = payload['outputSchemaResources']
        assert set(resources) == {skill_schemas.BASE + 'schemas/reason.schema.json'}
        assert 'private' not in json.dumps(resources)
        assert 'unrelated secret' not in json.dumps(payload)
        candidate['applicability']['reason'] = ''
        with pytest.raises(Exception):
            _candidate(candidate, validator, refs)


def test_helper_resolves_registered_contract_and_internal_fragment_without_network():
    registered = json.loads((ROOT / 'schemas/common/goal-context.schema.json').read_text(encoding='utf-8'))
    files = {'schemas/main.schema.json': json.dumps({
        '$schema': skill_schemas.DIALECT, 'type': 'object',
        'properties': {'context': {'$ref': registered['$id']},
                       'name': {'$ref': '#/$defs/name'}},
        '$defs': {'name': {'type': 'string', 'minLength': 2}}})}
    validator = skill_schemas.package_validator(files, 'schemas/main.schema.json')
    validator.validate({'context': {'goalId': 'g', 'scope': 'goal'}, 'name': 'ok'})
    with pytest.raises(Exception):
        validator.validate({'context': {'goalId': 'g', 'scope': 'subgoal'}, 'name': 'ok'})
    with pytest.raises(Exception):
        validator.validate({'name': ''})
    material = skill_schemas.package_schema_materials(files, 'schemas/main.schema.json')
    assert registered['$id'] in material['resources']


def test_unknown_external_reference_is_rejected_offline(monkeypatch):
    import urllib.request
    def forbidden(*args, **kwargs):
        pytest.fail('network resolution attempted')
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    files = {'schemas/main.schema.json': json.dumps({
        '$schema': skill_schemas.DIALECT, '$ref': 'https://outside.invalid/schema.json'})}
    validator = skill_schemas.package_validator(files, 'schemas/main.schema.json')
    with pytest.raises(Exception):
        validator.validate('value')
    with pytest.raises(Exception):
        skill_schemas.package_schema_materials(files, 'schemas/main.schema.json')


def test_transitive_resources_and_annotations_are_stripped_without_losing_property_names():
    files = {
        'schemas/main.schema.json': json.dumps({'$schema': skill_schemas.DIALECT,
            '$ref': 'a.schema.json', 'examples': [{'private': 1}]}),
        'schemas/a.schema.json': json.dumps({'$schema': skill_schemas.DIALECT, 'type': 'object',
            'properties': {'default': {'$ref': 'b.schema.json'}, 'examples': {'type': 'string'}},
            'default': {'private': 2}}),
        'schemas/b.schema.json': json.dumps({'$schema': skill_schemas.DIALECT, 'type': 'string',
            'examples': ['private'], 'default': 'private'}),
        'schemas/unused.schema.json': json.dumps({'$schema': skill_schemas.DIALECT,
                                                  'description': 'unrelated'}),
    }
    material = skill_schemas.package_schema_materials(files, 'schemas/main.schema.json')
    assert set(material['resources']) == {skill_schemas.BASE + 'schemas/a.schema.json',
                                           skill_schemas.BASE + 'schemas/b.schema.json'}
    a = material['resources'][skill_schemas.BASE + 'schemas/a.schema.json']
    assert set(a['properties']) == {'default', 'examples'}
    assert 'default' not in a
    assert 'private' not in json.dumps(material)
    assert 'unrelated' not in json.dumps(material)


def test_annotation_removal_does_not_change_const_object_values():
    files = {'schemas/main.schema.json': json.dumps({'$schema': skill_schemas.DIALECT,
        'const': {'default': 'required-value', 'examples': [1, 2]},
        'default': {'private': 1}})}
    material = skill_schemas.package_schema_materials(files, 'schemas/main.schema.json')
    assert material['schema']['const'] == {'default': 'required-value', 'examples': [1, 2]}
    assert 'default' not in material['schema']


def test_referenced_extension_schema_collects_transitive_resources_and_strips_annotations():
    files = {
        'schemas/main.schema.json': json.dumps({
            '$schema': skill_schemas.DIALECT, '$ref': '#/custom/value',
            'custom': {'value': {'$ref': 'child.schema.json',
                                 'examples': [{'private': True}], 'default': {'private': True}}}}),
        'schemas/child.schema.json': json.dumps({
            '$schema': skill_schemas.DIALECT, 'type': 'object',
            'properties': {'default': {'type': 'string', 'default': 'private'},
                           'fixed': {'const': {'default': 'preserve', 'examples': [1]}},
                           'choice': {'enum': [{'default': 'preserve'}]},
                           'next': {'$ref': 'main.schema.json'}},
            'examples': [{'private': True}], 'default': {'private': True}}),
    }
    skill_schemas.check_schemas(files, {'inputSchemaRef': 'schemas/main.schema.json',
                                       'outputSchemaRef': 'schemas/main.schema.json'})
    material = skill_schemas.package_schema_materials(files, 'schemas/main.schema.json')
    assert set(material['resources']) == {skill_schemas.BASE + 'schemas/child.schema.json'}
    assert material['schema']['custom']['value'] == {'$ref': 'child.schema.json'}
    child = material['resources'][skill_schemas.BASE + 'schemas/child.schema.json']
    assert 'private' not in json.dumps(material)
    assert child['properties']['default'] == {'type': 'string'}
    assert child['properties']['fixed']['const'] == {'default': 'preserve', 'examples': [1]}
    assert child['properties']['choice']['enum'] == [{'default': 'preserve'}]
