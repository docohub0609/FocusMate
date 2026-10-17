"""Offline package acceptance is not semantic or tutoring verification."""
import importlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))


def check(path):
    return importlib.import_module('focusmate.core.skill_package').validate_skill_package(path)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def seal(path):
    report = check(path)
    assert report['computedHash'], report
    descriptor = json.loads((path / 'descriptor.json').read_text(encoding='utf-8'))
    descriptor['hash'] = report['computedHash']
    dump(path / 'descriptor.json', descriptor)
    return check(path)


@pytest.fixture
def package(tmp_path):
    path = tmp_path / 'tree-and-graph'
    path.mkdir()
    (path / 'schemas').mkdir()
    (path / 'references').mkdir()
    (path / 'SKILL.md').write_text('''---
name: tree-and-graph
description: 对照图题条件检查当前代码与运行证据。
---
# 图题分析
## 适用条件
适用于图题代码分析；不适用于仅涉及数组求和的题。
## 目标
找出有依据的疑点，保留未知。
## 所需输入
本次提供的代码与题目条件。
## 处理方法
阅读[检查说明](references/checks.md)，结合已有证据分析。
## 禁止行为
不提供完整解答，不修改 GoalState，不伪造运行结果。
## 结果格式
见[输出格式](schemas/result.schema.json)。
''', encoding='utf-8')
    (path / 'references/checks.md').write_text('先核对题目是否允许自环。\n', encoding='utf-8')
    schema = {'$schema': 'https://json-schema.org/draft/2020-12/schema', 'type': 'object',
              'properties': {'text': {'type': 'string'}}, 'required': ['text'],
              'additionalProperties': False, 'examples': [{'text': '本次材料'}]}
    dump(path / 'schemas/input.schema.json', schema)
    dump(path / 'schemas/result.schema.json', schema)
    dump(path / 'descriptor.json', {
        'skillId': 'tree-and-graph', 'schemaVersion': '1.0.0', 'version': 1,
        'createdAt': '2026-09-13T00:00:00Z', 'hash': 'sha256:' + '0' * 64,
        'appliesTo': ['graph'], 'capabilities': ['structuredEvidence'],
        'requiredInputs': ['problem', 'code'], 'requiredTools': [], 'goalStateWriteAllowed': False,
        'inputSchemaRef': 'schemas/input.schema.json', 'outputSchemaRef': 'schemas/result.schema.json',
        'coachPolicy': {'completeSolutionAllowed': False}})
    return path


def codes(report):
    return {issue['code'] for issue in report['issues']}


def test_package_is_read_only_and_does_not_contact_network(package, monkeypatch):
    assert seal(package)['packageValid']
    before = {p.relative_to(package): p.read_bytes() for p in package.rglob('*') if p.is_file()}
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network'))
    report = check(package)
    assert report['packageValid']
    assert report['toolAvailability'] == 'notChecked'
    assert report['contentReview'] == 'notPerformed'
    assert before == {p.relative_to(package): p.read_bytes() for p in package.rglob('*') if p.is_file()}


@pytest.mark.parametrize('file', ['SKILL.md', 'schemas/input.schema.json', 'references/checks.md'])
def test_changed_file_invalidates_hash(package, file):
    seal(package)
    p = package / file
    p.write_text(p.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    assert 'HASH_MISMATCH' in codes(check(package))


def test_tool_declaration_is_bound_but_never_authorized(package):
    old = seal(package)['computedHash']
    p = package / 'descriptor.json'
    d = json.loads(p.read_text(encoding='utf-8'))
    d['requiredTools'] = ['future.graph.inspect']
    dump(p, d)
    assert 'HASH_MISMATCH' in codes(check(package))
    report = seal(package)
    assert report['packageValid'] and report['computedHash'] != old
    assert report['declaredToolRequirements'] == ['future.graph.inspect']
    assert report['toolAvailability'] == 'notChecked'


def test_hash_survives_move_and_windows_newlines(package, tmp_path):
    old = seal(package)['computedHash']
    other = tmp_path / 'other' / package.name
    shutil.copytree(package, other)
    for p in other.rglob('*'):
        if p.is_file():
            p.write_bytes(p.read_text(encoding='utf-8').replace('\n', '\r\n').encode('utf-8'))
    assert check(other)['packageValid']
    assert check(other)['computedHash'] == old


@pytest.mark.parametrize('change', ['missing', 'empty', 'fenced', 'duplicate'])
def test_real_nonempty_unique_sections_required(package, change):
    p = package / 'SKILL.md'
    text = p.read_text(encoding='utf-8')
    section = '## 目标\n找出有依据的疑点，保留未知。\n'
    replacements = {'missing': '', 'empty': '## 目标\n\n', 'fenced': '```md\n' + section + '```\n',
                    'duplicate': section + section}
    p.write_text(text.replace(section, replacements[change]), encoding='utf-8')
    assert 'INVALID_SECTIONS' in codes(check(package))


@pytest.mark.parametrize('field,value', [('skillId', 'other'), ('goalStateWriteAllowed', True),
    ('coachPolicy', {'completeSolutionAllowed': True}), ('requiredTools', ['x', 'x'])])
def test_invalid_descriptor(package, field, value):
    p = package / 'descriptor.json'
    d = json.loads(p.read_text(encoding='utf-8'))
    d[field] = value
    dump(p, d)
    assert not check(package)['packageValid']


@pytest.mark.parametrize('field', ['requiredTools', 'goalStateWriteAllowed', 'coachPolicy'])
def test_new_package_requires_explicit_boundaries(package, field):
    p = package / 'descriptor.json'
    d = json.loads(p.read_text(encoding='utf-8'))
    del d[field]
    dump(p, d)
    assert 'INVALID_DESCRIPTOR' in codes(check(package))


@pytest.mark.parametrize('file,content', [
    ('descriptor.json', '{"version":1,"version":2}'),
    ('schemas/input.schema.json', '{"type":"object","examples":[NaN]}'),
    ('SKILL.md', '---\nname: tree-and-graph\nname: other\ndescription: x\n---\n'),
    ('SKILL.md', '---\nname: &n tree-and-graph\ndescription: *n\n---\n'),
    ('SKILL.md', '---\nname: tree-and-graph\ndescription: x\nallowed-tools: Bash\n---\n')])
def test_strict_parsing(package, file, content):
    (package / file).write_text(content, encoding='utf-8')
    assert not check(package)['packageValid']


@pytest.mark.parametrize('ref', ['../../outside.json', 'https://invalid.example/schema.json', '#/$defs/missing', 'missing.json'])
def test_bad_schema_references_even_without_examples(package, ref):
    dump(package / 'schemas/result.schema.json', {'$schema': 'https://json-schema.org/draft/2020-12/schema',
        'type': 'object', 'properties': {'child': {'$ref': ref}}})
    assert 'INVALID_SCHEMA' in codes(check(package))


def test_local_and_registered_schema_references(package):
    dump(package / 'schemas/result.schema.json', {'$schema': 'https://json-schema.org/draft/2020-12/schema',
        'type': 'object', 'properties': {'value': {'$ref': 'input.schema.json'}, 'context': {
        '$ref': 'https://focusmate.local/schemas/common/goal-context.schema.json'}},
        'examples': [{'value': {'text': 'ok'}, 'context': {'goalId': 'g', 'scope': 'goal'}}]})
    assert seal(package)['packageValid']


@pytest.mark.parametrize('bad', [{'type': 'imaginary'}, {'type': 'string', 'examples': [1]}])
def test_schema_and_its_examples_must_validate(package, bad):
    dump(package / 'schemas/result.schema.json', {'$schema': 'https://json-schema.org/draft/2020-12/schema', **bad})
    assert 'INVALID_SCHEMA' in codes(check(package))


@pytest.mark.parametrize('link', ['../../outside.md', 'references/missing.md', 'file:///etc/passwd'])
def test_bad_markdown_links(package, link):
    p = package / 'references/checks.md'
    p.write_text('[材料](' + link + ')\n', encoding='utf-8')
    assert 'INVALID_REFERENCE' in codes(check(package))


def test_no_executables_or_symlinks(package, tmp_path):
    (package / 'run.py').write_text('raise Exception()', encoding='utf-8')
    assert 'UNSUPPORTED_FILE' in codes(check(package))
    (package / 'run.py').unlink()
    target = tmp_path / 'outside.txt'
    target.write_text('external', encoding='utf-8')
    try:
        (package / 'references/external.txt').symlink_to(target)
    except OSError:
        pytest.skip('host does not permit symlinks')
    assert 'UNSAFE_PATH' in codes(check(package))


def test_size_limit_and_missing_directory(package, tmp_path):
    (package / 'references/huge.txt').write_bytes(b'x' * (256 * 1024 + 1))
    assert 'PACKAGE_LIMIT' in codes(check(package))
    assert not check(tmp_path / 'absent')['packageValid']


def test_semantic_review_is_not_faked(package):
    p = package / 'SKILL.md'
    p.write_text(p.read_text(encoding='utf-8').replace('找出有依据的疑点，保留未知。', '这段文字的语义不由格式检查器判断。'), encoding='utf-8')
    assert seal(package)['contentReview'] == 'notPerformed'


def test_committed_example_and_cli(tmp_path):
    sample = ROOT / 'examples/skill-package/tree-and-graph'
    assert check(sample)['packageValid']
    cli = ROOT / 'tools/check-skill.py'
    result = subprocess.run([sys.executable, str(cli), str(sample), '--json'], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['packageValid']
    bad = subprocess.run([sys.executable, str(cli), str(tmp_path / 'missing')], capture_output=True, text=True, encoding='utf-8')
    assert bad.returncode == 1
    assert '未通过' in bad.stdout and 'Traceback' not in bad.stderr


def test_ref_target_outside_standard_keyword_is_checked(package):
    dump(package / 'schemas/result.schema.json', {'$schema': 'https://json-schema.org/draft/2020-12/schema',
        'type': 'object', 'properties': {'value': {'$ref': '#/legacy'}},
        'legacy': {'$ref': 'https://invalid.example/not-present.json'}})
    assert 'INVALID_SCHEMA' in codes(check(package))


def test_ref_to_non_schema_is_rejected(package):
    dump(package / 'schemas/result.schema.json', {'$schema': 'https://json-schema.org/draft/2020-12/schema',
        'type': 'object', 'properties': {'value': {'$ref': '#/title'}}, 'title': 'not a schema'})
    assert 'INVALID_SCHEMA' in codes(check(package))


def test_recursive_schema_is_finite_and_valid(package):
    dump(package / 'schemas/result.schema.json', {'$schema': 'https://json-schema.org/draft/2020-12/schema',
        'type': 'object', 'properties': {'next': {'$ref': '#'}}, 'examples': [{'next': {}}]})
    assert seal(package)['packageValid']


def test_sample_result_does_not_accept_state_write():
    from jsonschema import Draft202012Validator
    p = ROOT / 'examples/skill-package/tree-and-graph/schemas/result.schema.json'
    schema = json.loads(p.read_text(encoding='utf-8'))
    assert not Draft202012Validator(schema).is_valid({'findings': [], 'unknowns': [], 'goalStateUpdate': {}})


def test_old_descriptor_remains_compatible(package):
    from focusmate.contracts.schema_validation import validate
    d = json.loads((package / 'descriptor.json').read_text(encoding='utf-8'))
    del d['requiredTools'], d['goalStateWriteAllowed']
    validate('SkillDescriptor', d)


def test_reparse_points_rejected_without_host_symlink_privilege(package, monkeypatch):
    from types import SimpleNamespace
    original = Path.lstat
    def metadata(path):
        info = original(path)
        if path.name == 'checks.md':
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, 'lstat', metadata)
    assert 'UNSAFE_PATH' in codes(check(package))


def test_overflow_json_numbers_rejected(package):
    (package / 'schemas/result.schema.json').write_text(
        '{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"number","minimum":1e999}', encoding='utf-8')
    assert 'INVALID_SCHEMA' in codes(check(package))


def test_nested_schema_examples_checked(package):
    dump(package / 'schemas/result.schema.json', {'$schema': 'https://json-schema.org/draft/2020-12/schema',
        'type': 'object', 'properties': {'value': {'type': 'string', 'examples': [1]}}})
    assert 'INVALID_SCHEMA' in codes(check(package))


def test_html_cannot_bypass_local_link_check(package):
    (package / 'references/checks.md').write_text('<a href="../../outside.md">material</a>', encoding='utf-8')
    assert 'INVALID_REFERENCE' in codes(check(package))
