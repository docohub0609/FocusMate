"""Exercise saved public task metadata through the organizer, with a fake runner."""
from datetime import datetime, timedelta, timezone
import sqlite3

from tests.python.test_submission_storage import prepared_repository
from tests.python.test_tool_public_runs import Runner
from services.oj_worker.public_samples import PublicTestBundle, PublicTestCase
from services.oj_worker.tool_public_runs import PublicRunService
from src.focusmate.adapters.observations import SQLiteObservationStore
from src.focusmate.core.event_organizer import EventOrganizer
from tests.python.test_event_organization import setup, save, event, organizer, at, GOAL


def test_saved_public_conditions_and_results_flow_into_repeat_summary(tmp_path):
    repo = prepared_repository(tmp_path)
    goal = repo.get_goal('goal-submit')
    code = repo.get_code_snapshot('goal-submit', 1)
    problem = goal['problemRef']
    bundle = PublicTestBundle('public-comparison', 1, problem['problemId'],
                             problem['problemVersion'], (PublicTestCase('one', b'5\n', b'5\n'),))
    runner = Runner()
    service = PublicRunService(repo.path, tmp_path / 'public-work', runner)
    results = []
    for turn in ('first', 'second'):
        context = {'loopId': turn, 'goalContext': code['goalContext'], 'problemRef': problem,
                   'deadlineAt': (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()}
        results.append(service.run(context, code, bundle))
        assert service.event_receipts[0]['status'] == 'recorded'
    store = SQLiteObservationStore(repo.path, public_task_db=service.path)
    organizer = EventOrganizer(store)
    organizer.process('goal-submit', now=datetime.now(timezone.utc) + timedelta(seconds=3))
    summary, = store.list_summaries('goal-submit')
    assert summary['count'] == 2
    assert summary['runIds'] == [r['runId'] for r in results]
    assert len(summary['observationIds']) == 2
    assert not store.list_failures('goal-submit')
    organizer.process('goal-submit')
    assert store.list_summaries('goal-submit') == [summary]
    assert runner.calls == 4  # Compile and run per genuine operation; organization executes nothing.
    with sqlite3.connect(repo.path) as db:
        assert db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0] == 0


def test_request_result_link_does_not_override_conflicting_code_versions(tmp_path):
    repo = setup(tmp_path)
    save(repo, 0.5, 'print(2)')
    problem = repo.get_goal(GOAL)['problemRef']
    event(repo, 'runRequested', {'codeVersion': 1, 'runMode': 'publicExample',
          'problemRef': problem, 'runId': 'same-id'}, 1)
    event(repo, 'publicRunFinished', {'codeVersion': 2, 'problemRef': problem,
          'runId': 'same-id', 'loopId': 'loop', 'status': 'succeeded', 'sampleStatus': 'passed'}, 2)
    store, worker = organizer(repo)
    worker.process(GOAL, now=at(5))
    result = next(o for o in store.list_observations(GOAL) if o['kind'] == 'publicRunFinished')
    assert 'requestObservationIds' not in result['evidence']
    assert 'REQUEST_OBSERVATION_UNAVAILABLE' in result['uncertainty']
