"""Organization behavior using real SQLite receipts and saved source versions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from apps.api.submission_storage import SubmissionRepository
from tests.python.test_submission_storage import prepared_repository, submission_request
from tests.python.test_raw_event_collection import simulated_event, install_development_contract
from src.focusmate.adapters.raw_events import SQLiteEventStore

BASE = datetime(2026, 9, 17, tzinfo=timezone.utc)
GOAL = 'goal-submit'


def at(seconds):
    return BASE + timedelta(seconds=seconds)


def setup(tmp_path):
    repo = prepared_repository(tmp_path)
    repo = SubmissionRepository(repo.path)
    with sqlite3.connect(repo.path) as db:
        db.execute('UPDATE raw_events SET received_at=?', (at(0).isoformat(),))
    return repo


def save(repo, seconds, source):
    snapshot = repo.create_code_snapshot(GOAL, source, 'python3')
    with sqlite3.connect(repo.path) as db:
        db.execute('UPDATE raw_events SET received_at=? WHERE receive_sequence=(SELECT MAX(receive_sequence) FROM raw_events)', (at(seconds).isoformat(),))
    return snapshot


def event(repo, name, payload, seconds, **changes):
    item = simulated_event(name, payload, subgoal=False)
    item.update(eventId=f'{name}-{seconds}', idempotencyKey=f'{name}-{seconds}', **changes)
    if name == 'publicRunFinished':
        item.update(source='toolExecutor', actor='system')
    receipt = SQLiteEventStore(repo.path, reference_resolver=lambda e: True, clock=lambda: at(seconds).isoformat()).receive(item, trusted_source=item['source'])
    assert receipt['status'] == 'recorded', receipt
    return item


def organizer(repo, **kwargs):
    from src.focusmate.adapters.observations import SQLiteObservationStore
    from src.focusmate.core.event_organizer import EventOrganizer
    from src.focusmate.contracts.observations import OrganizationPolicy
    store = SQLiteObservationStore(repo.path)
    return store, EventOrganizer(store, policy=OrganizationPolicy(**kwargs))


def test_quiet_boundary_references_sources_without_copying_code(tmp_path):
    repo = setup(tmp_path)
    save(repo, 1, 'print(2)\n'); save(repo, 2, 'print(3)\n')
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(3.999))
    assert store.list_observations(GOAL) == []
    assert store.list_pending(GOAL)[0]['endVersion'] == 3
    worker.process(GOAL, now=at(4))
    obs, = store.list_observations(GOAL)
    assert obs['kind'] == 'codeChange'
    assert len(obs['sourceEventIds']) == 3
    assert obs['evidence']['endVersion'] == 3
    assert obs['evidence']['startVersion'] is None
    assert 'BASELINE_UNAVAILABLE' in obs['uncertainty']
    assert 'sourceCode' not in json.dumps(obs)
    assert store.list_pending(GOAL) == []


def test_run_flushes_only_matching_scope_and_never_means_pass(tmp_path):
    repo = setup(tmp_path)
    item = event(repo, 'runRequested', {'codeVersion': 1, 'runMode': 'publicExample', 'problemRef': repo.get_goal(GOAL)['problemRef']}, 1)
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(1))
    observations = store.list_observations(GOAL)
    assert [o['kind'] for o in observations] == ['codeChange', 'runRequested']
    assert observations[0]['trigger'] == 'runRequested'
    assert observations[1]['sourceEventIds'] == [item['eventId']]
    assert observations[1]['priority'] == 'observationReady'


def test_backlog_is_replayed_by_receipt_time_before_wall_clock_expiry(tmp_path):
    repo = setup(tmp_path)
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(0))
    save(repo, 1, 'x=2'); save(repo, 2, 'x=3')
    reopened, worker = organizer(repo)
    worker.process(GOAL, now=at(1000))
    observations = reopened.list_observations(GOAL)
    assert len(observations) == 1
    assert len(observations[0]['sourceEventIds']) == 3


def test_real_quiet_gap_separates_backlogged_edits(tmp_path):
    repo = setup(tmp_path)
    save(repo, 5, 'print(2)\n'); save(repo, 6, 'print(3)\n')
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(10))
    first, second = store.list_observations(GOAL)
    assert first['evidence']['endVersion'] == 1
    assert second['evidence']['startVersion'] == 1
    assert second['delta']['changed'] is True
    assert '-print(input())' in second['delta']['diff']
    assert '+print(3)' in second['delta']['diff']


def test_max_window_splits_even_when_saves_do_not_stop(tmp_path):
    repo = setup(tmp_path)
    for second in range(1, 33): save(repo, second, f'x={second}')
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(32))
    first, = store.list_observations(GOAL)
    assert first['trigger'] == 'maximumWindow'
    assert first['evidence']['endVersion'] == 30
    assert store.list_pending(GOAL)[0]['startVersion'] == 30


def test_restart_preserves_policy_on_existing_window(tmp_path):
    repo = setup(tmp_path)
    store, worker = organizer(repo, quiet_seconds=5)
    worker.process(GOAL, now=at(0))
    store, worker = organizer(repo, quiet_seconds=1)
    worker.process(GOAL, now=at(2))
    assert store.list_observations(GOAL) == []
    worker.process(GOAL, now=at(5))
    assert len(store.list_observations(GOAL)) == 1


def test_unchanged_source_is_not_invented_progress(tmp_path):
    repo = setup(tmp_path)
    save(repo, 5, 'print(input())\n')
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(8))
    obs = store.list_observations(GOAL)[1]
    assert obs['delta']['changed'] is False
    assert obs['delta']['diff'] == ''


def test_diff_truncation_keeps_complete_source_references(tmp_path):
    repo = setup(tmp_path)
    save(repo, 5, '甲' * 2000)
    store, worker = organizer(repo, diff_max_bytes=128)
    worker.process(GOAL, now=at(8))
    obs = store.list_observations(GOAL)[1]
    assert obs['delta']['truncated'] is True
    assert len(obs['delta']['diff'].encode()) <= 128
    assert len(obs['evidence']['codeRefs']) == 2


def test_help_does_not_guess_latest_code_and_session_resume_opens_no_edit(tmp_path):
    repo = setup(tmp_path)
    install_development_contract(repo)
    event(repo, 'helpRequested', {'requestType': 'stuck', 'messageRef': 'msg'}, 1,
          goalContext={'goalId': GOAL, 'scope': 'subgoal', 'subgoalId': 'sg'}, contractVersion=1)
    pause = event(repo, 'sessionPaused', {}, 2, sessionRef='s1')
    event(repo, 'sessionResumed', {'previousPauseEventId': pause['eventId']}, 3, sessionRef='s1')
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(4))
    help_obs = next(o for o in store.list_observations(GOAL) if o['kind'] == 'helpRequested')
    assert 'codeVersion' not in help_obs['evidence']
    assert 'HELP_CODE_UNSPECIFIED' in help_obs['uncertainty']
    assert store.list_pending(GOAL) == []


def test_duplicate_read_and_concurrent_workers_do_not_repeat_observations(tmp_path):
    repo = setup(tmp_path)
    store, worker = organizer(repo)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: worker.process(GOAL, now=at(10)), range(8)))
    assert len(store.list_observations(GOAL)) == 1
    worker.process(GOAL, now=at(20))
    assert len(store.list_observations(GOAL)) == 1


def test_late_public_result_does_not_attach_to_latest_version(tmp_path):
    repo = setup(tmp_path)
    save(repo, 1, 'print(2)')
    event(repo, 'publicRunFinished', {'runId': 'r1', 'loopId': 'l1', 'codeVersion': 1, 'problemRef': repo.get_goal(GOAL)['problemRef'], 'status': 'succeeded', 'sampleStatus': 'failed'}, 2)
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(5))
    obs = next(o for o in store.list_observations(GOAL) if o['kind'] == 'publicRunFinished')
    assert obs['evidence']['codeVersion'] == 1
    assert obs['evidence']['sampleStatus'] == 'failed'
    assert store.list_summaries(GOAL) == []


def test_corrupt_event_is_quarantined_without_stalling_later_valid_events(tmp_path):
    repo = setup(tmp_path)
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE raw_events SET event_json='not-json'")
    save(repo, 1, 'print(2)')
    store, worker = organizer(repo)
    result = worker.process(GOAL, now=at(4))
    assert result['failures'] == 1
    assert len(store.list_failures(GOAL)) == 1
    obs, = store.list_observations(GOAL)
    assert len(obs['sourceEventIds']) == 1
    assert obs['evidence']['startVersion'] == 1


def test_storage_failure_rolls_back_pending_and_cursor(tmp_path):
    repo = setup(tmp_path)
    store, worker = organizer(repo)
    with sqlite3.connect(repo.path) as db:
        db.execute("CREATE TRIGGER fail_cursor BEFORE INSERT ON observation_cursors BEGIN SELECT RAISE(ABORT,'test disk failure'); END")
    with pytest.raises(sqlite3.Error): worker.process(GOAL, now=at(0))
    assert store.list_pending(GOAL) == []
    with sqlite3.connect(repo.path) as db:
        db.execute('DROP TRIGGER fail_cursor')
    worker.process(GOAL, now=at(0))
    assert len(store.list_pending(GOAL)) == 1


def test_observation_failure_does_not_close_pending(tmp_path):
    repo = setup(tmp_path)
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(0))
    with sqlite3.connect(repo.path) as db:
        db.execute("CREATE TRIGGER fail_obs BEFORE INSERT ON observations BEGIN SELECT RAISE(ABORT,'test disk failure'); END")
    with pytest.raises(sqlite3.Error): worker.process(GOAL, now=at(3))
    assert len(store.list_pending(GOAL)) == 1
    with sqlite3.connect(repo.path) as db: db.execute('DROP TRIGGER fail_obs')
    worker.process(GOAL, now=at(3))
    assert len(store.list_observations(GOAL)) == 1
    assert store.list_pending(GOAL) == []


def test_cli_once_uses_saved_events_and_has_no_goal_side_effect(tmp_path):
    repo = setup(tmp_path)
    before = repo.get_goal(GOAL)
    result = subprocess.run([sys.executable, 'tools/organize-events.py', '--db', str(repo.path), '--goal-id', GOAL], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert len(report['observations']) == 1
    assert repo.get_goal(GOAL) == before
    assert len(SQLiteEventStore(repo.path).list_goal(GOAL)) == 1


def change_scope(repo, version, **fields):
    with sqlite3.connect(repo.path) as db:
        rows = db.execute('SELECT event_id,event_json FROM raw_events').fetchall()
        for identifier, raw in rows:
            value = json.loads(raw)
            if value['eventName'] == 'codeSnapshotCreated' and value['payload']['codeVersion'] == version:
                value.update(fields)
                db.execute('UPDATE raw_events SET event_json=? WHERE event_id=?', (json.dumps(value), identifier))


def test_interleaved_scopes_do_not_attribute_other_scopes_edits(tmp_path):
    repo = setup(tmp_path); install_development_contract(repo)
    save(repo, 0.5, 'b=2'); save(repo, 1, 'a=3')
    change_scope(repo, 2, goalContext={'goalId': GOAL, 'scope': 'subgoal', 'subgoalId': 'sg'}, contractVersion=1)
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(4))
    goal_edits = [o for o in store.list_observations(GOAL) if o['goalContext']['scope'] == 'goal']
    assert len(goal_edits) == 2
    assert goal_edits[1]['evidence']['startVersion'] == 2
    assert len(goal_edits[1]['sourceEventIds']) == 1


def test_session_and_contract_versions_separate_groups(tmp_path):
    repo = setup(tmp_path); install_development_contract(repo)
    with sqlite3.connect(repo.path) as db:
        db.execute("INSERT INTO goal_contract_versions SELECT goal_id,2,status,compilation_json FROM goal_contract_versions")
    save(repo, 0.5, 'x=2'); save(repo, 1, 'x=3')
    for version, contract, session in [(1, 1, 's1'), (2, 2, 's1'), (3, 2, 's2')]:
        change_scope(repo, version, goalContext={'goalId': GOAL, 'scope': 'subgoal', 'subgoalId': 'sg'}, contractVersion=contract, sessionRef=session)
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(4))
    assert len(store.list_observations(GOAL)) == 3


@pytest.mark.parametrize('corruption,reason', [
    ('goal', 'EVENT_GOAL_MISMATCH'), ('id', 'EVENT_ID_MISMATCH'),
    ('version', 'CODE_REFERENCE_INVALID'), ('time', 'EVENT_PARSE_INVALID'),
    ('problem', 'EVENT_PROBLEM_MISMATCH'), ('kind', 'EVENT_KIND_UNSUPPORTED')])
def test_broken_saved_material_is_a_failure_not_an_observation(tmp_path, corruption, reason):
    repo = setup(tmp_path)
    with sqlite3.connect(repo.path) as db:
        value = json.loads(db.execute('SELECT event_json FROM raw_events').fetchone()[0])
        if corruption == 'goal': value['goalContext']['goalId'] = 'another'
        if corruption == 'id': value['eventId'] = 'another'
        if corruption == 'version': value['payload']['codeVersion'] = 999
        if corruption == 'problem': value['payload']['problemRef']['problemVersion'] = 999
        if corruption == 'kind': value['eventName'] = 'futureKind'
        if corruption == 'time': db.execute("UPDATE raw_events SET received_at='bad-time'")
        db.execute('UPDATE raw_events SET event_json=?', (json.dumps(value),))
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(10))
    assert store.list_observations(GOAL) == []
    assert store.list_failures(GOAL)[0]['reason'] == reason


def test_two_goals_have_independent_processing_positions(tmp_path):
    repo = setup(tmp_path)
    other = SubmissionRepository(repo.path, goal_id_factory=lambda: 'goal-other')
    other.create_goal(repo.get_goal(GOAL)['problemRef'])
    other.create_code_snapshot('goal-other', 'print(10)', 'python3')
    with sqlite3.connect(repo.path) as db:
        db.execute('UPDATE raw_events SET received_at=? WHERE goal_id=?', (at(1).isoformat(), 'goal-other'))
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(5))
    assert store.list_observations('goal-other') == []
    worker.process('goal-other', now=at(5))
    assert store.list_observations('goal-other')[0]['goalContext']['goalId'] == 'goal-other'


def test_submission_request_and_result_link_by_saved_submission(tmp_path):
    repo = setup(tmp_path)
    submission, _ = repo.create_submission(submission_request())
    with sqlite3.connect(repo.path) as db:
        db.execute("UPDATE raw_events SET received_at=? WHERE event_name='submissionRequested'", (at(1).isoformat(),))
        db.execute('CREATE TABLE oj_results(oj_result_id TEXT,submission_id TEXT,goal_id TEXT,code_version INTEGER,verdict TEXT)')
        db.execute('INSERT INTO oj_results VALUES(?,?,?,?,?)', ('oj1', submission['submissionId'], GOAL, 1, 'WA'))
    payload = {'submissionId': submission['submissionId'], 'ojResultId': 'oj1', 'codeVersion': 1, 'verdict': 'WA', 'problemRef': repo.get_goal(GOAL)['problemRef']}
    event(repo, 'ojResultProduced', payload, 2, source='ojWorker', actor='system')
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(5))
    observations = store.list_observations(GOAL)
    request = next(o for o in observations if o['kind'] == 'submissionRequested')
    result = next(o for o in observations if o['kind'] == 'ojResultProduced')
    assert result['evidence']['requestObservationIds'] == [request['observationId']]
    assert store.list_summaries(GOAL) == []


def test_explicit_run_id_links_result_without_guessing_by_version(tmp_path):
    repo = setup(tmp_path)
    problem = repo.get_goal(GOAL)['problemRef']
    event(repo, 'runRequested', {'codeVersion': 1, 'runMode': 'publicExample', 'problemRef': problem, 'runId': 'r1'}, 1)
    event(repo, 'publicRunFinished', {'runId': 'r1', 'loopId': 'l1', 'codeVersion': 1, 'problemRef': problem, 'status': 'succeeded', 'sampleStatus': 'passed'}, 2)
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(5))
    request = next(o for o in store.list_observations(GOAL) if o['kind'] == 'runRequested')
    result = next(o for o in store.list_observations(GOAL) if o['kind'] == 'publicRunFinished')
    assert result['evidence']['requestObservationIds'] == [request['observationId']]


def test_legacy_observation_and_event_can_still_be_read(tmp_path):
    from src.focusmate.contracts.observations import read_observation
    old = json.loads(Path('schemas/process/observation.schema.json').read_text(encoding='utf-8'))['examples'][0]
    assert read_observation(old) == old
    repo = setup(tmp_path)
    with sqlite3.connect(repo.path) as db:
        old_event = json.loads(db.execute('SELECT event_json FROM raw_events').fetchone()[0])
        old_event.update(eventVersion=1, schemaVersion='1.0.0', source='web', goalContext={'goalId': GOAL, 'scope': 'subgoal', 'subgoalId': 'legacy-sg'})
        old_event.pop('actor'); old_event['payload'].pop('problemRef'); old_event['payload']['draftState'] = 'draft'
        db.execute('UPDATE raw_events SET event_json=?', (json.dumps(old_event),))
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(3))
    assert store.list_observations(GOAL) == []
    assert store.list_failures(GOAL)[0]['reason'] == 'LEGACY_CONTRACT_UNAVAILABLE'


def public_fixture(repo, path, seconds, run_id, *, status='succeeded', conditions='a' * 64,
                   change=None, null_result=False):
    """Explicit simulated durable task, never a claimed real execution."""
    problem = repo.get_goal(GOAL)['problemRef']
    context = {'goalContext': {'goalId': GOAL, 'scope': 'goal'}, 'problemRef': problem,
               'codeVersion': 1, 'loopId': 'loop-' + run_id}
    result = {'runId': run_id, **context, 'status': status, 'sampleStatus': 'passed', 'caseResults': [{
        'testCaseId': 'case-1', 'outcome': 'passed', 'input': '1', 'expectedOutput': '1',
        'actualOutput': '1', 'error': '', 'inputTruncated': False, 'expectedOutputTruncated': False,
        'actualOutputTruncated': False, 'errorTruncated': False, 'terminationReason': 'exited', 'exitCode': 0}]}
    if change: change(result)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS tool_public_tasks(run_id TEXT PRIMARY KEY,context_json TEXT,result_json TEXT,status TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS tool_public_comparison(run_id TEXT PRIMARY KEY,conditions_hash TEXT)')
        db.execute('INSERT INTO tool_public_tasks VALUES(?,?,?,?)', (run_id, json.dumps(context), None if null_result else json.dumps(result), status))
        if conditions:
            db.execute('INSERT INTO tool_public_comparison VALUES(?,?)', (run_id, conditions))
    payload = {'runId': run_id, 'loopId': context['loopId'], 'codeVersion': 1, 'problemRef': problem, 'status': status}
    if not null_result: payload['sampleStatus'] = result['sampleStatus']
    event(repo, 'publicRunFinished', payload, seconds)


def public_worker(repo, path):
    from src.focusmate.adapters.observations import SQLiteObservationStore
    from src.focusmate.core.event_organizer import EventOrganizer
    store = SQLiteObservationStore(repo.path, public_task_db=path)
    return store, EventOrganizer(store)


def test_repeated_real_operations_summarize_without_rewriting_observations(tmp_path):
    repo = setup(tmp_path); path = tmp_path / 'public.db'
    public_fixture(repo, path, 1, 'r1')
    store, worker = public_worker(repo, path)
    worker.process(GOAL, now=at(2))
    original = store.list_observations(GOAL)
    assert store.list_summaries(GOAL) == []
    public_fixture(repo, path, 3, 'r2')
    worker.process(GOAL, now=at(4))
    summary, = store.list_summaries(GOAL)
    assert summary['count'] == 2
    assert summary['runIds'] == ['r1', 'r2']
    assert store.list_observations(GOAL)[:len(original)] == original
    worker.process(GOAL, now=at(5))
    assert store.list_summaries(GOAL)[0]['count'] == 2


@pytest.mark.parametrize('variation', ['conditions', 'output', 'truncated', 'missingMetadata', 'notExecuted'])
def test_similar_public_results_are_not_merged_without_complete_equality(tmp_path, variation):
    repo = setup(tmp_path); path = tmp_path / 'public.db'
    public_fixture(repo, path, 1, 'r1')
    def change(result):
        if variation == 'output': result['caseResults'][0]['actualOutput'] = '2'
        if variation == 'truncated': result['caseResults'][0]['actualOutputTruncated'] = True
        if variation == 'notExecuted': result['caseResults'][0]['outcome'] = 'notExecuted'
    public_fixture(repo, path, 2, 'r2', conditions='b'*64 if variation == 'conditions' else None if variation == 'missingMetadata' else 'a'*64, change=change)
    store, worker = public_worker(repo, path)
    worker.process(GOAL, now=at(4))
    assert len([o for o in store.list_observations(GOAL) if o['kind'] == 'publicRunFinished']) == 2
    assert store.list_summaries(GOAL) == []


@pytest.mark.parametrize('status', ['cancelled', 'interrupted'])
def test_cancelled_task_without_result_body_is_still_an_observed_fact(tmp_path, status):
    repo = setup(tmp_path); path = tmp_path / 'public.db'
    public_fixture(repo, path, 1, 'r1', status=status, null_result=True)
    store, worker = public_worker(repo, path)
    worker.process(GOAL, now=at(4))
    obs = next(o for o in store.list_observations(GOAL) if o['kind'] == 'publicRunFinished')
    assert obs['evidence']['status'] == status
    assert store.list_summaries(GOAL) == []


def test_task_database_io_failure_does_not_advance_event_cursor(tmp_path):
    repo = setup(tmp_path); path = tmp_path / 'public.db'
    public_fixture(repo, path, 1, 'r1')
    store, worker = public_worker(repo, tmp_path / 'does-not-exist.db')
    with pytest.raises(sqlite3.Error): worker.process(GOAL, now=at(4))
    assert store.list_observations(GOAL) == []
    assert len(store.list_pending(GOAL)) == 1
    good_store, good_worker = public_worker(repo, path)
    good_worker.process(GOAL, now=at(4))
    assert any(o['kind'] == 'publicRunFinished' for o in good_store.list_observations(GOAL))


@pytest.mark.parametrize('corruption', ['nullCase', 'resultArray', 'contextArray', 'wrongVersion'])
def test_broken_task_material_is_quarantined(tmp_path, corruption):
    repo = setup(tmp_path); path = tmp_path / 'public.db'
    public_fixture(repo, path, 1, 'r1')
    with sqlite3.connect(path) as db:
        raw, = db.execute('SELECT result_json FROM tool_public_tasks').fetchone()
        result = json.loads(raw)
        if corruption == 'nullCase': result['caseResults'] = [None]
        if corruption == 'resultArray': result = []
        if corruption == 'wrongVersion': result['codeVersion'] = 99
        if corruption == 'contextArray': db.execute("UPDATE tool_public_tasks SET context_json='[]'")
        db.execute('UPDATE tool_public_tasks SET result_json=?', (json.dumps(result),))
    store, worker = public_worker(repo, path)
    worker.process(GOAL, now=at(4))
    assert store.list_failures(GOAL)[0]['reason'] == 'PUBLIC_RESULT_REFERENCE_INVALID'
    assert all(o['kind'] != 'publicRunFinished' for o in store.list_observations(GOAL))


def test_observation_contract_rejects_incomplete_organization_metadata(tmp_path):
    from src.focusmate.contracts.observations import read_observation
    repo = setup(tmp_path); store, worker = organizer(repo)
    worker.process(GOAL, now=at(3))
    value = store.list_observations(GOAL)[0]
    value['organization']['policy'] = {}
    with pytest.raises(Exception): read_observation(value)
