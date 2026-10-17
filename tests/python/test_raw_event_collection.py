"""Real business references and explicitly simulated future dialogue references."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3

import pytest

from tests.python.test_submission_storage import prepared_repository, submission_request
from src.focusmate.adapters.raw_events import SQLiteEventStore, event_from_record
from src.focusmate.contracts.raw_events import read_event


def test_snapshot_and_submission_are_recorded_once(tmp_path):
    repo = prepared_repository(tmp_path)
    store = SQLiteEventStore(repo.path)
    first = store.list_goal('goal-submit')
    assert [r['event']['eventName'] for r in first] == ['codeSnapshotCreated']
    assert 'sourceCode' not in json.dumps(first)
    repo.create_submission(submission_request())
    repo.create_submission(submission_request())
    assert len(store.list_goal('goal-submit')) == 2


def test_retries_conflicts_and_late_receipts(tmp_path):
    repo = prepared_repository(tmp_path)
    store = SQLiteEventStore(repo.path)
    event = event_from_record('codeSnapshotCreated', dict(repo.get_code_snapshot('goal-submit', 1), problemRef=repo.get_goal('goal-submit')['problemRef']))
    assert store.receive(event, trusted_source='api')['status'] == 'duplicate'
    changed = deepcopy(event); changed['createdAt'] = '2020-01-01T00:00:00Z'
    assert store.receive(changed, trusted_source='api')['reason'] == 'EVENT_IDEMPOTENCY_CONFLICT'
    changed['idempotencyKey'] = 'late-independent'; changed['eventId'] = 'late-independent'
    assert store.receive(changed, trusted_source='api')['status'] == 'recorded'
    rows = store.list_goal('goal-submit', after_sequence=1)
    assert rows[0]['event']['createdAt'].startswith('2020')
    assert repo.get_code_snapshot('goal-submit', 1)['codeVersion'] == 1


@pytest.mark.parametrize('change', ['goal', 'version', 'source', 'secret', 'artifact'])
def test_invalid_references_and_private_fields_rejected(tmp_path, change):
    repo = prepared_repository(tmp_path)
    event = event_from_record('codeSnapshotCreated', dict(repo.get_code_snapshot('goal-submit', 1), problemRef=repo.get_goal('goal-submit')['problemRef']))
    if change == 'goal': event['goalContext']['goalId'] = 'another-goal'
    if change == 'version': event['payload']['codeVersion'] = 999
    if change == 'source': event['source'] = 'ojWorker'
    if change == 'secret': event['payload']['hiddenTests'] = 'PRIVATE_MARKER'
    if change == 'artifact': event['payload']['artifactRef'] = 'wrong'
    store = SQLiteEventStore(repo.path)
    assert store.receive(event, trusted_source='api')['status'] == 'rejected'
    assert len(store.list_goal('goal-submit')) == 1


def test_concurrent_duplicate_receipt(tmp_path):
    repo = prepared_repository(tmp_path)
    store = SQLiteEventStore(repo.path)
    event = event_from_record('codeSnapshotCreated', dict(repo.get_code_snapshot('goal-submit', 1), problemRef=repo.get_goal('goal-submit')['problemRef']))
    event.update(eventId='concurrent', idempotencyKey='concurrent')
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: store.receive(event, trusted_source='api'), range(8)))
    assert sum(r['status'] == 'recorded' for r in receipts) == 1
    assert sum(r['status'] == 'duplicate' for r in receipts) == 7


def test_concurrent_submission_writes_business_and_event_once(tmp_path):
    repo = prepared_repository(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: repo.create_submission(submission_request()), range(8)))
    assert sum(created for _, created in receipts) == 1
    assert len(SQLiteEventStore(repo.path).list_goal('goal-submit')) == 2


def test_actor_claim_and_bad_schema_never_reach_storage(tmp_path):
    repo = prepared_repository(tmp_path)
    event = event_from_record('codeSnapshotCreated', dict(repo.get_code_snapshot('goal-submit', 1), problemRef=repo.get_goal('goal-submit')['problemRef']))
    event['actor'] = 'model'
    store = SQLiteEventStore(repo.path)
    assert store.receive(event, trusted_source='api')['reason'] == 'EVENT_SOURCE_INVALID'
    event['actor'] = 'user'; event['payload']['apiKey'] = 'PRIVATE_SECRET'
    assert store.receive(event, trusted_source='api')['reason'] == 'EVENT_SCHEMA_INVALID'
    assert 'PRIVATE_SECRET' not in json.dumps(store.list_goal('goal-submit'))


def test_event_write_failure_rolls_back_business_write(tmp_path, monkeypatch):
    repo = prepared_repository(tmp_path)
    def fail(*args, **kwargs): raise sqlite3.OperationalError('simulated write failure')
    monkeypatch.setattr(SQLiteEventStore, 'append_on_connection', fail)
    with pytest.raises(sqlite3.OperationalError):
        repo.create_code_snapshot('goal-submit', 'print(2)', 'python3')
    with sqlite3.connect(repo.path) as db:
        assert db.execute('SELECT COUNT(*) FROM code_snapshots').fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError): repo.create_submission(submission_request())
    with sqlite3.connect(repo.path) as db:
        assert db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 0


def install_development_contract(repo):
    # Explicit fixture, not a claim that users have confirmed this contract.
    with sqlite3.connect(repo.path) as db:
        db.execute('CREATE TABLE goal_contract_versions(goal_id TEXT,version INTEGER,status TEXT,compilation_json TEXT)')
        db.execute('INSERT INTO goal_contract_versions VALUES(?,?,?,?)', ('goal-submit', 1, 'frozen',
            json.dumps({'contract': {'subgoals': [{'subgoalId': 'sg'}]}})))


def simulated_event(name, payload, *, subgoal=True):
    event = {'eventId': name, 'schemaVersion': '2.0.0', 'eventVersion': 2,
        'createdAt': '2026-09-17T00:00:00Z', 'goalContext': {'goalId': 'goal-submit', 'scope': 'goal'},
        'eventName': name, 'source': 'developerFixture', 'actor': 'simulated',
        'privacyLevel': 'taskContent', 'idempotencyKey': name, 'payload': payload}
    if subgoal:
        event['goalContext'].update(scope='subgoal', subgoalId='sg'); event['contractVersion'] = 1
    return event


def test_future_dialogue_sources_are_explicit_fixtures_and_resolved(tmp_path):
    repo = prepared_repository(tmp_path); install_development_contract(repo)
    # Resolver accepts only these known action/message/session references.
    def refs(e):
        p = e['payload']; name = e['eventName']
        if name == 'helpRequested': return p.get('messageRef') == 'message-1'
        if name == 'actionFeedbackProvided': return p['actionId'] == 'action-1'
        if name.startswith('session'): return e['sessionRef'] in ('session-1', 'session-2')
        return p.get('runMode') == 'publicExample'
    store = SQLiteEventStore(repo.path, reference_resolver=refs)
    help_event = simulated_event('helpRequested', {'requestType': 'stuck', 'messageRef': 'message-1'})
    assert store.receive(help_event, trusted_source='developerFixture')['status'] == 'recorded'
    bad = deepcopy(help_event); bad['payload']['messageRef'] = 'unknown'
    assert store.receive(bad, trusted_source='developerFixture')['reason'] == 'EVENT_REFERENCE_INVALID'
    feedback = simulated_event('actionFeedbackProvided', {'actionId': 'action-1', 'feedback': 'notHelpful'})
    assert store.receive(feedback, trusted_source='developerFixture')['status'] == 'recorded'
    run = simulated_event('runRequested', {'codeVersion': 1, 'runMode': 'publicExample', 'problemRef': repo.get_goal('goal-submit')['problemRef']})
    assert store.receive(run, trusted_source='developerFixture')['status'] == 'recorded'
    pause = simulated_event('sessionPaused', {}, subgoal=False); pause['sessionRef'] = 'session-1'
    assert store.receive(pause, trusted_source='developerFixture')['status'] == 'recorded'
    resume = simulated_event('sessionResumed', {'previousPauseEventId': pause['eventId']}, subgoal=False)
    resume['sessionRef'] = 'session-2'
    assert store.receive(resume, trusted_source='developerFixture')['status'] == 'rejected'
    resume['sessionRef'] = 'session-1'
    assert store.receive(resume, trusted_source='developerFixture')['status'] == 'recorded'


def test_unknown_subgoal_and_contract_version_fail(tmp_path):
    repo = prepared_repository(tmp_path); install_development_contract(repo)
    store = SQLiteEventStore(repo.path, reference_resolver=lambda _: True)
    event = simulated_event('helpRequested', {'requestType': 'hint'})
    event['contractVersion'] = 2
    assert store.receive(event, trusted_source='developerFixture')['reason'] == 'EVENT_CONTEXT_INVALID'
    event['contractVersion'] = 1; event['goalContext']['subgoalId'] = 'wrong'
    assert store.receive(event, trusted_source='developerFixture')['reason'] == 'EVENT_CONTEXT_INVALID'


def test_oj_record_is_source_of_truth_and_failure_rolls_back(tmp_path, monkeypatch):
    from services.oj_worker.judge import OJResultRepository, JudgeOutcome
    from tests.python.test_oj_judge import item, bundle
    repo = prepared_repository(tmp_path); repo.create_submission(submission_request())
    results = OJResultRepository(repo.path)
    original = SQLiteEventStore.append_on_connection
    def fail(*args, **kwargs): raise sqlite3.OperationalError('simulated disk failure')
    monkeypatch.setattr(SQLiteEventStore, 'append_on_connection', fail)
    with pytest.raises(sqlite3.OperationalError): results.save(item(), bundle((b'PRIVATE_INPUT', b'PRIVATE_OUTPUT')), JudgeOutcome('AC', 1, None, 1, None, None))
    assert results.get_for_submission('submission-submit') is None
    monkeypatch.setattr(SQLiteEventStore, 'append_on_connection', original)
    result = results.save(item(), bundle((b'PRIVATE_INPUT', b'PRIVATE_OUTPUT')), JudgeOutcome('AC', 1, None, 1, None, None))
    event = event_from_record('ojResultProduced', result)
    event['payload']['verdict'] = 'WA'
    store = SQLiteEventStore(repo.path)
    assert store.receive(event, trusted_source='ojWorker')['reason'] == 'EVENT_REFERENCE_INVALID'
    text = json.dumps(store.list_goal('goal-submit'))
    assert 'PRIVATE_' not in text and 'testSummary' not in text and 'stderr' not in text


def test_legacy_schema_read_remains_supported():
    from pathlib import Path
    legacy = json.loads((Path(__file__).resolve().parents[2] / 'schemas/process/raw-event.schema.json').read_text(encoding='utf-8'))['examples'][0]
    assert read_event(legacy) == legacy


def test_model_request_identity_is_set_by_trusted_caller(tmp_path):
    from src.focusmate.core.raw_event_collector import RawEventCollector
    repo = prepared_repository(tmp_path)
    event = simulated_event('runRequested', {'codeVersion': 1, 'runMode': 'publicExample',
        'problemRef': repo.get_goal('goal-submit')['problemRef']}, subgoal=False)
    event.update(source='api', actor='model')
    store = SQLiteEventStore(repo.path, reference_resolver=lambda _: True)
    collector = RawEventCollector(store)
    assert collector.collect(event, trusted_source='api')['status'] == 'rejected'
    assert collector.collect(event, trusted_source='api', trusted_actor='model')['status'] == 'recorded'
    assert collector.query('goal-submit')[-1]['event']['actor'] == 'model'
    with sqlite3.connect(repo.path) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='tool_public_tasks'").fetchone()


def test_public_backfill_retries_delivery_not_execution(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from services.oj_worker.tool_public_runs import PublicRunService
    from services.oj_worker.public_samples import PublicTestBundle, PublicTestCase
    from tests.python.test_tool_public_runs import Runner
    repo = prepared_repository(tmp_path); runner = Runner()
    service = PublicRunService(repo.path, tmp_path / 'runs', runner)
    context = {'loopId': 'public-loop', 'goalContext': {'goalId': 'goal-submit', 'scope': 'goal'},
        'problemRef': repo.get_goal('goal-submit')['problemRef'],
        'deadlineAt': (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()}
    bundle = PublicTestBundle('public', 1, 'problem-submit', 1, (PublicTestCase('example', b'', b'5\n'),))
    original = SQLiteEventStore.receive
    monkeypatch.setattr(SQLiteEventStore, 'receive', lambda *a, **kw: {'status': 'rejected', 'reason': 'EVENT_STORAGE_UNAVAILABLE'})
    outcome = service.run(context, repo.get_code_snapshot('goal-submit', 1), bundle)
    assert outcome['status'] == 'succeeded'
    assert service.event_receipts[0]['status'] == 'rejected'
    count = runner.calls
    monkeypatch.setattr(SQLiteEventStore, 'receive', original)
    assert service.record_events()[0]['status'] == 'recorded'
    assert service.record_events()[0]['status'] == 'duplicate'
    assert runner.calls == count
    event = SQLiteEventStore(repo.path).list_goal('goal-submit')[-1]['event']
    assert event['eventName'] == 'publicRunFinished'
    assert event['payload']['sampleStatus'] == 'passed'
    assert 'caseResults' not in event['payload']
    with sqlite3.connect(repo.path) as db:
        assert db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 0


def test_subgoal_public_event_keeps_original_contract_version(tmp_path):
    from datetime import datetime, timedelta, timezone
    from services.oj_worker.tool_public_runs import PublicRunService
    from services.oj_worker.public_samples import PublicTestBundle, PublicTestCase
    from tests.python.test_tool_public_runs import Runner
    repo = prepared_repository(tmp_path); install_development_contract(repo)
    service = PublicRunService(repo.path, tmp_path / 'runs', Runner())
    context = {'loopId': 'subgoal-run', 'goalContext': {'goalId': 'goal-submit', 'scope': 'subgoal', 'subgoalId': 'sg'},
        'problemRef': repo.get_goal('goal-submit')['problemRef'],
        'deadlineAt': (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()}
    bundle = PublicTestBundle('public', 1, 'problem-submit', 1, (PublicTestCase('ex', b'', b'5\n'),))
    service.run(context, repo.get_code_snapshot('goal-submit', 1), bundle)
    assert service.event_receipts[0]['event']['contractVersion'] == 1
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE goal_contract_versions SET status='superseded'")
        db.execute('INSERT INTO goal_contract_versions VALUES(?,?,?,?)', ('goal-submit', 2, 'frozen', json.dumps({'contract': {'subgoals': [{'subgoalId': 'sg-new'}]}})))
    assert service.record_events()[0]['status'] == 'duplicate'
    assert service.event_receipts[0]['event']['contractVersion'] == 1


def test_historical_public_task_without_terminal_time_is_not_fabricated(tmp_path):
    from services.oj_worker.tool_public_runs import PublicRunService
    from tests.python.test_tool_public_runs import Runner
    repo = prepared_repository(tmp_path)
    service = PublicRunService(repo.path, tmp_path / 'runs', Runner())
    with sqlite3.connect(service.path) as db:
        db.execute('INSERT INTO tool_public_tasks VALUES(?,?,?,?,?,?,?,?,?)',
            ('legacy-run', 'legacy-key', 'failed', None, 0, '{}', None, None, str(tmp_path / 'runs' / 'legacy-run')))
    assert service.record_events()[0]['reason'] == 'EVENT_SOURCE_TIME_UNAVAILABLE'
    assert len(SQLiteEventStore(repo.path).list_goal('goal-submit')) == 1
