from copy import deepcopy
from importlib import import_module

import pytest

from apps.api.problem_intake import create_problem_package
from apps.api.problem_storage import ProblemRepository, StorageFailure
from tests.python.test_problem_features import INTAKE
from tests.python.test_problem_storage import feature_result


def setup_job(tmp_path):
    repo = ProblemRepository(tmp_path / 'db')
    record = repo.create(create_problem_package(INTAKE))
    jobs = import_module('apps.api.feature_jobs').FeatureJobs(repo)
    request = {'problemId': record['problemPackage']['problemId'], 'problemVersion': 1,
               'expectedDraftRevision': 1, 'contentHash': record['lifecycle']['contentHash']}
    return repo, record, jobs, request


def test_duplicate_job_and_active_version_do_not_enqueue_twice(tmp_path):
    repo, record, jobs, request = setup_job(tmp_path)
    first = jobs.enqueue(request, 'parse-1', 'owner')
    assert jobs.enqueue(request, 'parse-1', 'owner') == first
    assert jobs.enqueue(request, 'parse-2', 'owner')['jobId'] == first['jobId']
    claim = jobs.claim('owner')
    assert jobs.claim('owner') is None
    result = feature_result(record['problemPackage'])
    jobs.complete(claim, result)
    saved = jobs.get(first['jobId'])
    assert saved['status'] == 'succeeded'
    assert repo.current_feature_set(request['problemId'], 1)['featureSetId'] == saved['featureSetId']


def test_late_results_are_stale_and_do_not_write_report(tmp_path):
    repo, record, jobs, request = setup_job(tmp_path)
    job = jobs.enqueue(request, 'parse-1', 'owner')
    claim = jobs.claim('owner')
    repo.update_draft(request['problemId'], 1, record['problemPackage'], 1)
    jobs.complete(claim, feature_result(record['problemPackage']))
    assert jobs.get(job['jobId'])['status'] == 'stale'
    with pytest.raises(StorageFailure):
        repo.current_feature_set(request['problemId'], 1)


def test_restart_interrupts_old_job_and_rejects_old_executor(tmp_path):
    repo, record, jobs, request = setup_job(tmp_path)
    job = jobs.enqueue(request, 'parse-1', 'old')
    claim = jobs.claim('old')
    jobs.recover('new')
    assert jobs.get(job['jobId'])['status'] == 'interrupted'
    jobs.complete(claim, feature_result(record['problemPackage']))
    assert jobs.get(job['jobId'])['status'] == 'interrupted'
    assert jobs.claim('new') is None
    with pytest.raises(StorageFailure):
        repo.current_feature_set(request['problemId'], 1)


def test_changed_key_content_rejected_and_transaction_rolls_back(tmp_path, monkeypatch):
    repo, record, jobs, request = setup_job(tmp_path)
    job = jobs.enqueue(request, 'parse-1', 'owner')
    with pytest.raises(StorageFailure) as caught:
        jobs.enqueue({**request, 'expectedDraftRevision': 2}, 'parse-1', 'owner')
    assert caught.value.code == 'IDEMPOTENCY_CONFLICT'
    claim = jobs.claim('owner')
    original = repo._save_feature_set
    def broken(*args):
        original(*args)
        raise RuntimeError('after report insert')
    monkeypatch.setattr(repo, '_save_feature_set', broken)
    with pytest.raises(RuntimeError):
        jobs.complete(claim, feature_result(record['problemPackage']))
    assert jobs.get(job['jobId'])['status'] == 'running'
    with pytest.raises(StorageFailure):
        repo.current_feature_set(request['problemId'], 1)
