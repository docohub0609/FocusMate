"""Registry discovery is read-only and never selects a version implicitly."""
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_skill_package import ROOT, package, seal, dump
from focusmate.core.skill_registry import SkillRegistry


def test_metadata_filters_and_copies(package):
    seal(package)
    registry = SkillRegistry()
    report = registry.refresh([package.parent])
    assert report['refreshSucceeded'] and report['allValid']
    entry = registry.get(package.name, 1)
    assert entry['name'] == package.name and entry['description'].startswith('对照图题')
    assert entry['sources'] == [str(package.absolute())]
    assert entry['contentReview'] == 'notPerformed'
    assert entry['toolAvailability'] == 'notChecked'
    assert 'body' not in entry and 'fileManifest' not in entry
    assert registry.list(capability='structuredEvidence', applies_to='graph') == [entry]
    assert registry.list(capability='structured') == []
    assert registry.list(applies_to='Graph') == []
    assert registry.get(package.name, 2) is None
    entry['coachPolicy']['completeSolutionAllowed'] = True
    registry.list()[0]['capabilities'].clear()
    assert registry.get(package.name, 1)['coachPolicy']['completeSolutionAllowed'] is False
    assert registry.list(capability='structuredEvidence')


def test_duplicates_conflicts_and_versions(package, tmp_path):
    seal(package)
    other = tmp_path / 'other' / package.name
    shutil.copytree(package, other)
    registry = SkillRegistry()
    report = registry.refresh([package.parent, other.parent])
    # A nested root is also a malformed direct child of the first root.
    assert report['refreshSucceeded'] and not report['allValid']
    assert len(registry.get(package.name, 1)['sources']) == 2
    p = other / 'SKILL.md'
    p.write_text(p.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    seal(other)
    report = registry.refresh([package.parent, other.parent])
    assert registry.get(package.name, 1) is None
    assert any(i['code'] == 'IDENTITY_CONFLICT' for i in report['issues'])
    descriptor = json.loads((other / 'descriptor.json').read_text(encoding='utf-8'))
    descriptor['version'] = 10
    dump(other / 'descriptor.json', descriptor)
    seal(other)
    registry.refresh([other.parent, package.parent])
    assert [e['version'] for e in registry.list()] == [1, 10]


def test_atomic_refresh_and_bad_package(package, tmp_path, monkeypatch):
    seal(package)
    registry = SkillRegistry()
    registry.refresh([package.parent])
    old = registry.list()
    assert not registry.refresh([tmp_path / 'missing'])['refreshSucceeded']
    assert registry.list() == old
    original = Path.iterdir
    def denied(path):
        if path == package.parent:
            raise PermissionError('denied')
        return original(path)
    monkeypatch.setattr(Path, 'iterdir', denied)
    assert not registry.refresh([package.parent])['refreshSucceeded']
    assert registry.list() == old
    monkeypatch.setattr(Path, 'iterdir', original)
    (package.parent / 'README.md').write_text('ignored')
    (package.parent / 'empty').mkdir()
    report = registry.refresh([package.parent])
    assert report['refreshSucceeded'] and not report['allValid']
    assert registry.list() == old
    shutil.rmtree(package)
    registry.refresh([package.parent])
    assert registry.list() == []


@pytest.mark.parametrize('target', ['root', 'package'])
def test_reparse_rejected(package, monkeypatch, target):
    seal(package)
    original = Path.lstat
    def metadata(path):
        info = original(path)
        if path == (package.parent if target == 'root' else package):
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, 'lstat', metadata)
    registry = SkillRegistry()
    report = registry.refresh([package.parent])
    assert not report['allValid'] and not registry.list()
    assert report['refreshSucceeded'] == (target == 'package')


def test_metadata_uses_same_read(package, monkeypatch):
    seal(package)
    import focusmate.core.skill_package as module
    original = module.read_files
    calls = []
    def read(path):
        calls.append(path)
        files = original(path)
        (package / 'SKILL.md').write_text('changed after read', encoding='utf-8')
        return files
    monkeypatch.setattr(module, 'read_files', read)
    registry = SkillRegistry()
    assert registry.refresh([package.parent])['allValid']
    assert len(calls) == 1 and registry.list()[0]['description'].startswith('对照图题')


def test_default_and_cli(package):
    assert SkillRegistry().refresh()['allValid']
    seal(package)
    command = [sys.executable, str(ROOT / 'tools/list-skills.py'), str(package.parent), '--json']
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)['skills']) == 1
    result = subprocess.run(command + ['--capability', 'absent'], capture_output=True, text=True, encoding='utf-8')
    assert json.loads(result.stdout)['skills'] == []
    (package / 'SKILL.md').write_text('bad', encoding='utf-8')
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 1 and not json.loads(result.stdout)['allValid']


def test_read_only_and_no_network(package, monkeypatch):
    seal(package)
    before = {p: p.read_bytes() for p in package.rglob('*') if p.is_file()}
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network'))
    registry = SkillRegistry()
    report = registry.refresh([package.parent])
    report['skills'][0]['sources'].clear()
    assert registry.get(package.name, 1)['sources']
    assert before == {p: p.read_bytes() for p in package.rglob('*') if p.is_file()}
    with pytest.raises(TypeError):
        registry.get(package.name, True)
    with pytest.raises(TypeError):
        registry.get(package.name, '1')


def test_cli_missing_root_and_chinese(package):
    command = [sys.executable, str(ROOT / 'tools/list-skills.py'), str(package / 'absent')]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 1 and '刷新失败' in result.stdout and 'Traceback' not in result.stderr


def test_empty_root_is_valid(tmp_path):
    registry = SkillRegistry()
    report = registry.refresh([tmp_path])
    assert report['allValid'] and report['refreshSucceeded'] and report['skills'] == []
