from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import sqlite3
import sys
from threading import Barrier

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))


def storage_module():
    assert (ROOT / 'apps/api/goal_contract_storage.py').is_file(), 'T042 needs durable contract lifecycle storage'
    return importlib.import_module('apps.api.goal_contract_storage')


@pytest.fixture
def seeded(tmp_path):
    from tests.python.goal_compilation_fixtures import stored_inputs
    from tests.python.test_goal_compilation import Provider
    from focusmate.core.goal_compilation import compile_goal_contract
    path = tmp_path / 'contracts.db'
    inputs = stored_inputs(path, 'merge-intervals')
    compilation = compile_goal_contract(*inputs, Provider(), created_at='2026-09-12T02:00:00Z')
    return path, inputs, compilation


@pytest.fixture
def repo(seeded):
    return storage_module().GoalContractRepository(seeded[0], clock=lambda: datetime(2026, 9, 12, 3, tzinfo=timezone.utc))


def install_review(monkeypatch, *, status='passed', callback=None):
    """Inject controlled network output; review protocol and storage stay real."""
    module = storage_module()
    from focusmate.core.goal_contract_review import review_goal_contract
    from tests.python.test_goal_contract_review import Reviewer
    calls = []
    def review(goal, problem, features, compilation, provider, *, created_at):
        snapshot = module.validate_review_input(goal, problem, features, compilation)
        calls.append(snapshot)
        if callback:
            callback()
        def flag(value, payload):
            value['items'][0].update(verdict='insufficient', issueTypes=['insufficientEvidence'], blocking=True)
        fake = Reviewer(raw='invalid JSON') if status == 'unavailable' else Reviewer(mutate=flag if status == 'needsReview' else None)
        return review_goal_contract(goal, problem, features, compilation, fake, created_at=created_at)
    monkeypatch.setattr(module, 'review_goal_contract', review)
    return calls


def imported(repo, seeded):
    return repo.import_draft(seeded[1][0]['goalId'], seeded[2])


def freeze(repo, record):
    return repo.review_and_freeze(record['goalId'], record['version'], object(), expected_draft_revision=record['draftRevision'])


def test_missing_database_is_not_fabricated(tmp_path):
    path = tmp_path / 'missing.db'
    module = storage_module()
    with pytest.raises(Exception) as caught:
        module.GoalContractRepository(path)
    assert caught.value.code == 'GOAL_INPUT_UNAVAILABLE'
    assert not path.exists()


def test_import_owns_lifecycle_preserves_original_and_never_changes_goal(repo, seeded):
    path, inputs, compilation = seeded
    original = deepcopy(compilation)
    with sqlite3.connect(path) as db:
        before = db.execute('SELECT * FROM goal_records').fetchall()
    compilation['contract'].update(version=99, contractStatus='frozen', createdAt='2000-01-01T00:00:00Z')
    record = imported(repo, seeded)
    assert (record['version'], record['draftRevision'], record['status']) == (1, 1, 'draft')
    assert record['compilation']['contract']['createdAt'] == '2026-09-12T03:00:00+00:00'
    assert record['originalGeneration'] == compilation
    assert record['compilation']['generation']['semanticStatus'] == 'unverified'
    assert repo.get_current(record['goalId']) is None
    assert record['basedOnVersion'] is None and record['reviews'] == []
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM goal_records').fetchall() == before
    assert record['compilation']['rubrics'] == original['rubrics']


def test_real_inputs_load_as_single_consistent_snapshot(seeded):
    assert storage_module().load_inputs(seeded[0], seeded[1][0]['goalId']) == seeded[1]


def test_edit_snapshot_immutable_and_old_revision_cannot_overwrite(repo, seeded):
    record = imported(repo, seeded)
    editable = repo.export_editable(record['goalId'], 1)
    editable['subgoals'][0]['userGoal'] = '说明题目要求的区间端点条件'
    changed = repo.edit_draft(record['goalId'], 1, editable, expected_draft_revision=1)
    assert changed['draftRevision'] == 2 and changed['contentHash'] != record['contentHash']
    assert changed['originalGeneration'] == record['originalGeneration']
    assert repo.get_draft_revision(record['goalId'], 1, 1)['compilation'] == record['compilation']
    with pytest.raises(Exception) as caught:
        repo.edit_draft(record['goalId'], 1, editable, expected_draft_revision=1)
    assert caught.value.code == 'STALE_GOAL_CONTRACT_DRAFT'
    assert repo.get_version(record['goalId'], 1)['draftRevision'] == 2


@pytest.mark.parametrize('field', ['goalId', 'version', 'status', 'sourceCatalog', 'generation', 'coachPolicy', 'strategyCandidates'])
def test_editor_rejects_protected_top_level_fields(repo, seeded, field):
    record = imported(repo, seeded)
    editable = repo.export_editable(record['goalId'], 1)
    editable[field] = 'forged'
    with pytest.raises(Exception) as caught:
        repo.edit_draft(record['goalId'], 1, editable, expected_draft_revision=1)
    assert caught.value.code == 'INVALID_GOAL_CONTRACT_EDIT'
    assert repo.get_version(record['goalId'], 1)['contentHash'] == record['contentHash']


def test_editor_adds_and_removes_nodes_with_server_ids_and_safe_links(repo, seeded):
    record = imported(repo, seeded)
    editable = repo.export_editable(record['goalId'], 1)
    extra = deepcopy(editable['subgoals'][0])
    extra.update(key='new:boundary-detail', stage='boundaryAndTesting', acceptableEvidence=['targetedRunResult'],
                 objective='检查区间端点相等时的输出', userGoal='检查区间端点相等时是否满足输出要求')
    extra['dependencies'] = [{'key': editable['subgoals'][0]['key'], 'reason': '需要先明确题目要求', 'sourceRefs': ['statement:0']}]
    editable['subgoals'].append(extra)
    changed = repo.edit_draft(record['goalId'], 1, editable, expected_draft_revision=1)
    node = changed['compilation']['contract']['subgoals'][-1]
    assert node['subgoalId'] != 'new:boundary-detail'
    assert node['dependsOn'] == [record['compilation']['contract']['subgoals'][0]['subgoalId']]
    assert changed['compilation']['rubrics'][-1]['sources'] == record['compilation']['rubrics'][0]['sources']
    edited_again = repo.export_editable(record['goalId'], 1)
    edited_again['subgoals'].pop()
    removed = repo.edit_draft(record['goalId'], 1, edited_again, expected_draft_revision=2)
    assert len(removed['compilation']['contract']['subgoals']) == 5
    assert repo.get_draft_revision(record['goalId'], 1, 2)['compilation']['contract']['subgoals'][-1] == node


@pytest.mark.parametrize('change', ['unknownRef', 'injectedSource', 'foreignId', 'dependency', 'evidence'])
def test_edit_cannot_forge_sources_links_or_stage_evidence(repo, seeded, change):
    record = imported(repo, seeded)
    editable = repo.export_editable(record['goalId'], 1)
    node = editable['subgoals'][0]
    if change == 'unknownRef': node['sourceRefs'] = ['private:0']
    elif change == 'injectedSource': node['checks'][0]['sources'] = [{'sourceText': 'fabricated'}]
    elif change == 'foreignId': node['key'] = 'other-goal:subgoal'
    elif change == 'dependency': node['dependencies'] = [{'key': 'missing', 'reason': 'requires', 'sourceRefs': ['statement:0']}]
    else: node['acceptableEvidence'] = ['runResult']
    with pytest.raises(Exception):
        repo.edit_draft(record['goalId'], 1, editable, expected_draft_revision=1)
    assert repo.get_version(record['goalId'], 1)['draftRevision'] == 1


def test_freeze_is_idempotent_and_cannot_edit_frozen(repo, seeded, monkeypatch):
    calls = install_review(monkeypatch)
    record = imported(repo, seeded)
    frozen = freeze(repo, record)
    assert frozen['status'] == 'frozen' and frozen['frozenAt']
    assert frozen['compilation']['contract']['contractStatus'] == 'frozen'
    assert frozen['contentHash'] == record['contentHash']
    assert freeze(repo, frozen) == frozen
    assert len(calls) == 1 and len(frozen['reviews']) == 1
    assert repo.get_current(record['goalId']) == frozen
    with pytest.raises(Exception) as caught:
        repo.edit_draft(record['goalId'], 1, repo.export_editable(record['goalId'], 1), expected_draft_revision=1)
    assert caught.value.code == 'GOAL_CONTRACT_IMMUTABLE'


@pytest.mark.parametrize('status', ['needsReview', 'unavailable'])
def test_failed_new_version_preserves_active_contract_and_history(repo, seeded, monkeypatch, status):
    install_review(monkeypatch)
    old = freeze(repo, imported(repo, seeded))
    draft = repo.create_revision(old['goalId'], 1, '补充区间边界的目标')
    install_review(monkeypatch, status=status)
    failed = freeze(repo, draft)
    assert failed['status'] == 'draft' and failed['reviews'][-1]['result']['status'] == status
    assert repo.get_current(old['goalId']) == old
    assert (failed['version'], failed['basedOnVersion'], failed['changeReason']) == (2, 1, '补充区间边界的目标')


def test_new_freeze_supersedes_only_metadata_old_content_and_refs_survive(repo, seeded, monkeypatch):
    install_review(monkeypatch)
    old = freeze(repo, imported(repo, seeded))
    next_draft = repo.create_revision(old['goalId'], 1, '澄清验收条件')
    new = freeze(repo, next_draft)
    previous = repo.get_version(old['goalId'], 1)
    assert previous['status'] == 'superseded' and previous['supersededAt']
    assert previous['compilation'] == old['compilation'] and previous['contentHash'] == old['contentHash']
    assert repo.get_current(old['goalId']) == new
    assert [item['version'] for item in repo.list_versions(old['goalId'])] == [1, 2]
    subgoal_id = old['compilation']['contract']['subgoals'][0]['subgoalId']
    ref = repo.resolve_contract_reference(old['goalId'], 1, subgoal_id)
    assert ref['goalContractVersion'] == 1 and ref['contentHash'] == old['contentHash']
    assert ref['subgoal']['subgoalId'] == subgoal_id
    with pytest.raises(Exception): repo.resolve_contract_reference(old['goalId'], 2, 'unknown')
    with pytest.raises(Exception): repo.create_revision(old['goalId'], 1, '旧版不可作为新基线')


def test_review_of_draft_edited_during_model_call_is_retained_but_cannot_freeze(repo, seeded, monkeypatch):
    record = imported(repo, seeded)
    def edit_during_review():
        editable = repo.export_editable(record['goalId'], 1)
        editable['subgoals'][0]['userGoal'] = '说明题目要求的区间端点条件'
        repo.edit_draft(record['goalId'], 1, editable, expected_draft_revision=1)
    install_review(monkeypatch, callback=edit_during_review)
    with pytest.raises(Exception) as caught: freeze(repo, record)
    assert caught.value.code == 'STALE_GOAL_CONTRACT_REVIEW'
    latest = repo.get_version(record['goalId'], 1)
    assert latest['status'] == 'draft' and latest['draftRevision'] == 2
    assert latest['reviews'][-1]['stale'] is True
    assert latest['reviews'][-1]['draftRevision'] == 1
    assert repo.get_current(record['goalId']) is None


def test_competing_initial_reviews_cannot_both_become_current(repo, seeded, monkeypatch):
    one = imported(repo, seeded)
    two = imported(repo, seeded)
    barrier = Barrier(2)
    install_review(monkeypatch, callback=lambda: barrier.wait(timeout=5))
    def attempt(record):
        try: return freeze(repo, record)['status']
        except Exception as error: return error.code
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(attempt, [one, two]))
    assert sorted(results) == ['STALE_GOAL_CONTRACT_REVIEW', 'frozen']
    assert len([row for row in repo.list_versions(one['goalId']) if row['status'] == 'frozen']) == 1


def test_failure_saving_review_rolls_back_supersession(repo, seeded, monkeypatch):
    install_review(monkeypatch)
    old = freeze(repo, imported(repo, seeded))
    new = repo.create_revision(old['goalId'], 1, '补充说明')
    with sqlite3.connect(seeded[0]) as db:
        db.execute("CREATE TRIGGER fail_contract_review BEFORE INSERT ON goal_contract_reviews BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END")
    with pytest.raises(Exception): freeze(repo, new)
    assert repo.get_current(old['goalId']) == old
    assert repo.get_version(old['goalId'], 2)['status'] == 'draft'
    assert repo.get_version(old['goalId'], 2)['reviews'] == []


def test_asset_status_changes_do_not_invalidate_draft_or_progress(repo, seeded, monkeypatch):
    install_review(monkeypatch)
    record = imported(repo, seeded)
    with sqlite3.connect(seeded[0]) as db:
        before = db.execute('SELECT * FROM goal_records').fetchall()
        db.execute("UPDATE problem_versions SET verifier_asset_status='ready', updated_at='2026-09-12T03:30:00Z'")
    frozen = freeze(repo, record)
    assert frozen['status'] == 'frozen'
    with sqlite3.connect(seeded[0]) as db:
        assert db.execute('SELECT * FROM goal_records').fetchall() == before


def test_record_schema_covers_draft_frozen_superseded_and_has_example(repo, seeded, monkeypatch):
    from focusmate.contracts.schema_validation import validate, _catalog
    install_review(monkeypatch)
    draft = imported(repo, seeded)
    frozen = freeze(repo, draft)
    freeze(repo, repo.create_revision(draft['goalId'], 1, '调整目标'))
    superseded = repo.get_version(draft['goalId'], 1)
    _catalog.cache_clear()
    for record in (draft, frozen, superseded): validate('GoalContractRecord', record)
    schema = json.loads((ROOT / 'schemas/goal/goal-contract-record.schema.json').read_text(encoding='utf-8'))
    assert schema.get('examples')
    for example in schema['examples']: validate('GoalContractRecord', example)


def test_editor_does_not_offer_fixed_overall_objective(repo, seeded):
    record = imported(repo, seeded)
    editable = repo.export_editable(record['goalId'], 1)
    assert 'objective' not in editable
    editable['objective'] = 'Replace the fixed final learning goal'
    with pytest.raises(Exception):
        repo.edit_draft(record['goalId'], 1, editable, expected_draft_revision=1)
    assert repo.get_version(record['goalId'], 1)['contentHash'] == record['contentHash']


def test_removed_node_prunes_current_strategy_refs_but_preserves_history(repo, seeded):
    from tests.python.test_goal_compilation import wire, Provider
    from focusmate.core.goal_compilation import compile_goal_contract
    candidate = wire()
    extra = deepcopy(candidate['subgoals'][0])
    extra['key'] = 'extra'
    candidate['subgoals'].append(extra)
    candidate['strategyCandidates'] = [{'label': '扫描方向', 'approach': '逐项检查', 'applicability': '规模允许时',
        'uncertainties': ['开销待验证'], 'subgoalKeys': ['extra'], 'sourceRefs': ['constraints:0']}]
    package = compile_goal_contract(*seeded[1], Provider(candidate), created_at='2026-09-12T02:00:00Z')
    record = repo.import_draft(seeded[1][0]['goalId'], package)
    editable = repo.export_editable(record['goalId'], 1)
    editable['subgoals'].pop()
    edited = repo.edit_draft(record['goalId'], 1, editable, expected_draft_revision=1)
    assert edited['compilation']['strategyCandidates'] == []
    assert edited['originalGeneration']['strategyCandidates'] == package['strategyCandidates']
    assert repo.get_draft_revision(record['goalId'], 1, 1)['compilation']['strategyCandidates'] == package['strategyCandidates']
    with sqlite3.connect(seeded[0]) as db:
        saved = json.loads(db.execute('SELECT candidates_json FROM goal_contract_strategy_history').fetchone()[0])
    assert saved == package['strategyCandidates']


def test_concurrent_same_draft_freezes_once_and_retains_both_model_reviews(repo, seeded, monkeypatch):
    record = imported(repo, seeded)
    barrier = Barrier(2)
    calls = install_review(monkeypatch, callback=lambda: barrier.wait(timeout=5))
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: freeze(repo, record), range(2)))
    assert all(result['status'] == 'frozen' for result in results)
    latest = repo.get_version(record['goalId'], 1)
    assert len(calls) == 2 and len(latest['reviews']) == 2
    assert sum(item['stale'] for item in latest['reviews']) == 1
    assert repo.get_draft_revision(record['goalId'], 1, 1)['reviews'] == []
    assert repo.get_draft_revision(record['goalId'], 1, 1)['status'] == 'draft'


def test_persisted_invalid_review_cannot_escape_output_contract(repo, seeded):
    record = imported(repo, seeded)
    with sqlite3.connect(seeded[0]) as db:
        db.execute('INSERT INTO goal_contract_reviews (goal_id,version,draft_revision,stale,result_json,created_at) VALUES (?,1,1,0,?,?)',
                   (record['goalId'], '{"status":"passed","private":"RAW MODEL OUTPUT"}', record['createdAt']))
    with pytest.raises(Exception) as caught:
        repo.get_version(record['goalId'], 1)
    assert caught.value.code == 'INVALID_GOAL_CONTRACT_RECORD'
    assert 'RAW MODEL OUTPUT' not in str(caught.value)
