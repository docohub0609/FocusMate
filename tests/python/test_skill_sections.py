"""Selective bodies must stay inside an explicitly pinned package."""
import importlib
import json

import pytest
from test_skill_package import ROOT, package, seal, dump


def loader():
    return importlib.import_module('focusmate.core.skill_sections')


@pytest.fixture
def indexed(package):
    items = []
    for sid in ('visit-control', 'coverage-termination', 'path-cost'):
        (package / 'references' / (sid + '.md')).write_text('ONLY ' + sid, encoding='utf-8')
        items.append({'sectionId': sid, 'description': 'Check ' + sid,
                      'fileRef': 'references/' + sid + '.md'})
    dump(package / 'references/index.json', {'skillId': 'tree-and-graph', 'version': 1, 'sections': items})
    seal(package)
    return package


def test_selected_content_excludes_other_sections(indexed):
    module = loader()
    snap = module.load_index(indexed, 1)
    assert snap.skill_id == 'tree-and-graph' and snap.version == 1
    assert len(snap.sections) == 3 and not hasattr(snap, 'common')
    result = module.load_sections(snap, ['visit-control'])
    assert result['sections'] == {'visit-control': 'ONLY visit-control'}
    assert '## 禁止行为' in result['common']
    assert 'ONLY path-cost' not in json.dumps(result)
    assert len(module.load_sections(snap, ['visit-control', 'path-cost'])['sections']) == 2


@pytest.mark.parametrize('ids', [[], ['bad'], ['visit-control'] * 2,
    ['visit-control', 'coverage-termination', 'path-cost'], 'visit-control', [None]])
def test_selection_rejects_unknown_duplicate_or_unbounded_ids(indexed, ids):
    module = loader()
    with pytest.raises(ValueError):
        module.load_sections(module.load_index(indexed, 1), ids)


def test_explicit_version_hash_and_drift(indexed):
    module = loader()
    snap = module.load_index(indexed, 1)
    for version in (True, '1', 2):
        with pytest.raises((ValueError, TypeError)):
            module.load_index(indexed, version)
    with pytest.raises(ValueError):
        module.load_index(indexed, 1, 'sha256:' + '0' * 64)
    (indexed / 'references/path-cost.md').write_text('changed', encoding='utf-8')
    seal(indexed)  # even a newly valid package cannot substitute an existing snapshot
    with pytest.raises(ValueError):
        module.load_sections(snap, ['visit-control'])


@pytest.mark.parametrize('path', ['../../outside.md', '/outside.md', 'https://example.com/x',
    'references/missing.md', 'references/../SKILL.md', 'references/path-cost.md#x',
    'references%2fpath-cost.md', 'references\\path-cost.md'])
def test_index_rejects_unsafe_or_noncanonical_paths(indexed, path):
    index = json.loads((indexed / 'references/index.json').read_text(encoding='utf-8'))
    index['sections'][0]['fileRef'] = path
    dump(indexed / 'references/index.json', index)
    seal(indexed)
    with pytest.raises(ValueError):
        loader().load_index(indexed, 1)


def test_index_identity_and_duplicate_ids_are_rejected(indexed):
    target = indexed / 'references/index.json'
    value = json.loads(target.read_text(encoding='utf-8'))
    value['sections'][1]['sectionId'] = value['sections'][0]['sectionId']
    dump(target, value)
    seal(indexed)
    with pytest.raises(ValueError):
        loader().load_index(indexed, 1)
    value['sections'].pop(1)
    value['version'] = 2
    dump(target, value)
    seal(indexed)
    with pytest.raises(ValueError):
        loader().load_index(indexed, 1)


def test_graph_v7_package_and_three_sections():
    module = loader()
    snap = module.load_index(ROOT / 'skills/tree-and-graph', 7)
    assert {s.section_id for s in snap.sections} == {'visit-control', 'coverage-termination', 'path-cost'}

@pytest.mark.parametrize('change', ['unsealed', 'index-resealed', 'missing-index'])
def test_snapshot_rejects_changed_or_missing_package_files(indexed, change):
    module = loader()
    snapshot = module.load_index(indexed, 1)
    if change == 'unsealed':
        (indexed / 'references/visit-control.md').write_text('tampered', encoding='utf-8')
    elif change == 'index-resealed':
        target = indexed / 'references/index.json'
        value = json.loads(target.read_text(encoding='utf-8'))
        value['sections'][0]['description'] = 'different routing meaning'
        dump(target, value)
        seal(indexed)
    else:
        (indexed / 'references/index.json').unlink()
        seal(indexed)
    with pytest.raises(ValueError):
        module.load_sections(snapshot, ['visit-control'])


@pytest.mark.parametrize('value', [None, [], {'skillId': 'other', 'version': 1, 'sections': []},
    {'skillId': 'tree-and-graph', 'version': True, 'sections': []}])
def test_malformed_index_is_rejected(indexed, value):
    dump(indexed / 'references/index.json', value)
    seal(indexed)
    with pytest.raises(ValueError):
        loader().load_index(indexed, 1)
