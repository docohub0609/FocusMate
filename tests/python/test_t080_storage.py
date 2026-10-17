from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from apps.api.problem_intake import create_problem_package
from apps.api.problem_storage import ProblemRepository, StorageFailure
from tests.python.test_problem_features import INTAKE
from tests.python.test_problem_storage import feature_result


def test_optional_title_roundtrip_and_retry_conflict(tmp_path):
    repo = ProblemRepository(tmp_path / 'db')
    problem = repo.create(create_problem_package({**INTAKE, 'title': '两数之和'}), operation_key='named')['problemPackage']
    assert problem['title'] == '两数之和'
    assert repo.list_problems()['items'][0]['title'] == '两数之和'
    with pytest.raises(StorageFailure) as error:
        repo.create(create_problem_package({**INTAKE, 'title': '另一个名字'}), operation_key='named')
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    updated = repo.update_draft(problem['problemId'], 1, {**problem, 'title': '新名字'}, 1)
    assert updated['problemPackage']['title'] == '新名字'
    accepted = feature_result(updated['problemPackage'])
    repo.save_feature_set(accepted)
    repo.freeze(problem['problemId'], 1, accepted['featureSetId'], 2)
    assert repo.create_revision(problem['problemId'], 1)['problemPackage']['title'] == '新名字'


def test_delete_is_idempotent_and_restorable_without_losing_records(tmp_path):
    repo = ProblemRepository(tmp_path / 'db')
    original = repo.create(create_problem_package(INTAKE))
    pid = original['problemPackage']['problemId']
    first = repo.set_deleted(pid, True)
    assert repo.set_deleted(pid, True) == first
    assert repo.list_problems()['items'] == []
    assert repo.list_problems(deleted=True)['items'][0]['problemId'] == pid
    assert repo.get(pid, 1) == original
    repo.set_deleted(pid, False)
    assert repo.list_problems()['items'][0]['problemId'] == pid
    assert repo.list_problems(deleted=True)['items'] == []
    with pytest.raises(StorageFailure) as error:
        repo.set_deleted('missing', True)
    assert error.value.code == 'PROBLEM_NOT_FOUND'


def test_creation_retry_returns_same_problem_across_connections(tmp_path):
    path = tmp_path / 'db.sqlite'
    repo = ProblemRepository(path)
    def create(_):
        return ProblemRepository(path).create(create_problem_package(INTAKE), operation_key='save-one')
    with ThreadPoolExecutor(2) as pool:
        first, second = list(pool.map(create, range(2)))
    assert first == second
    assert len(repo.list_problems()['items']) == 1
    with pytest.raises(StorageFailure) as caught:
        repo.create(create_problem_package({**INTAKE, 'statement': 'Another problem'}), operation_key='save-one')
    assert caught.value.code == 'IDEMPOTENCY_CONFLICT'


def test_list_returns_latest_version_once_and_paginates(tmp_path):
    repo = ProblemRepository(tmp_path / 'db')
    for _ in range(3):
        repo.create(create_problem_package(INTAKE))
    first = repo.list_problems(limit=2)
    second = repo.list_problems(limit=2, cursor=first['nextCursor'])
    assert len(first['items']) == 2 and len(second['items']) == 1
    assert {x['problemId'] for x in first['items']}.isdisjoint(x['problemId'] for x in second['items'])


def test_late_model_result_cannot_replace_frozen_report(tmp_path):
    repo = ProblemRepository(tmp_path / 'db')
    record = repo.create(create_problem_package(INTAKE))
    problem = record['problemPackage']
    accepted = feature_result(problem)
    late = feature_result(problem)
    repo.save_feature_set(accepted)
    repo.freeze(problem['problemId'], 1, accepted['featureSetId'], 1)
    with pytest.raises(StorageFailure):
        repo.save_feature_set(late)
    assert repo.confirmed_feature_set(problem['problemId'], 1) == accepted


def test_old_revision_rejected_even_when_content_hash_unchanged(tmp_path):
    repo = ProblemRepository(tmp_path / 'db')
    record = repo.create(create_problem_package(INTAKE))
    problem = record['problemPackage']
    result = feature_result(problem)
    repo.update_draft(problem['problemId'], 1, deepcopy(problem), 1)
    with pytest.raises(StorageFailure) as caught:
        repo.save_feature_set(result, expected_revision=1)
    assert caught.value.code == 'DRAFT_REVISION_CONFLICT'


def test_two_draft_writers_cannot_silently_overwrite(tmp_path):
    repo = ProblemRepository(tmp_path / 'db')
    problem = repo.create(create_problem_package(INTAKE))['problemPackage']
    def update(text):
        try:
            repo.update_draft(problem['problemId'], 1, {**problem, 'statement': text}, 1)
            return 'saved'
        except StorageFailure as error:
            return error.code
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(update, ['first edit', 'second edit']))
    assert sorted(outcomes) == ['DRAFT_REVISION_CONFLICT', 'saved']
    assert repo.get(problem['problemId'], 1)['lifecycle']['draftRevision'] == 2
