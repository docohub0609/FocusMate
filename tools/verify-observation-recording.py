"""Demonstrate T061 against real saves and a real Docker public run in an isolated DB."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src'), str(ROOT / 'tools')]
from goal_compilation_examples import seed_example
from apps.api.submission_storage import SubmissionRepository
from services.oj_worker.tool_public_runs import PublicRunService
from focusmate.adapters.raw_events import SQLiteEventStore


def now():
    return datetime.now(timezone.utc)


def business_snapshot(path):
    """Compare the entire saved business state without copying its contents to the report."""
    with sqlite3.connect(path) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                 if r[0] in {'raw_events', 'code_snapshots', 'goal_records', 'submissions', 'oj_results', 'goal_contract_versions'}]
        return {name: db.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall()
                for name in names}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    from focusmate.adapters.observations import SQLiteObservationStore
    from focusmate.core.event_organizer import EventOrganizer, OrganizationPolicy

    path = args.output / 'fixture.sqlite3'
    goal, frozen, _ = seed_example(path, 'two-sum')
    goal_id = goal['goalId']
    repo = SubmissionRepository(path)
    source = ('n, target = map(int, input().split())\n'
              'a = list(map(int, input().split()))\nseen = {}\n'
              'for j, value in enumerate(a):\n'
              '    if target - value in seen:\n'
              '        print(seen[target - value], j)\n        break\n'
              '    seen[value] = j\n')
    policy = OrganizationPolicy(quiet_seconds=2, max_seconds=30, diff_max_bytes=8192)
    store = SQLiteObservationStore(path)
    organizer = EventOrganizer(store, policy=policy)
    repo.create_code_snapshot(goal_id, '# original saved version\n' + source, 'python3')
    organizer.process(goal_id, now=now())
    # This is an actual elapsed quiet period; stored event receipt times are not rewritten.
    time.sleep(2.1)
    organizer.process(goal_id, now=now())
    repo.create_code_snapshot(goal_id, '# second saved version\n' + source, 'python3')
    code3 = repo.create_code_snapshot(goal_id, '# third saved version\n' + source, 'python3')
    organizer.process(goal_id, now=now())
    pending_before_restart = store.list_pending(goal_id)
    # New objects reconnect to the persisted SQLite state; no in-memory state is reused.
    store = SQLiteObservationStore(path)
    organizer = EventOrganizer(store, policy=policy)
    pending_after_restart = store.list_pending(goal_id)
    repo.create_code_snapshot(goal_id, '# fourth saved version\n' + source, 'python3')
    click = {'eventId': 'event-' + uuid4().hex, 'schemaVersion': '2.0.0', 'eventVersion': 2,
             'createdAt': now().isoformat(), 'goalContext': code3['goalContext'],
             'eventName': 'runRequested', 'source': 'developerFixture', 'actor': 'simulated',
             'idempotencyKey': 't061-demo-public-click', 'privacyLevel': 'taskContent',
             'payload': {'codeVersion': code3['codeVersion'], 'runMode': 'publicExample',
                         'problemRef': goal['problemRef']}}
    receipt = SQLiteEventStore(path, reference_resolver=lambda event: event == click).receive(
        click, trusted_source='developerFixture')
    organizer.process(goal_id, now=now())
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE public_test_cases(test_case_id TEXT,asset_id TEXT,asset_version INTEGER,problem_id TEXT,problem_version INTEGER,example_index INTEGER,stdin_text TEXT,expected_output_text TEXT)')
        for i, example in enumerate(frozen['problemPackage']['examples']):
            db.execute('INSERT INTO public_test_cases VALUES(?,?,?,?,?,?,?,?)',
                       (f'example-{i}', 'developer-public-examples', 1,
                        goal['problemRef']['problemId'], goal['problemRef']['problemVersion'],
                        i, example['input'], example['output']))
    context = {'loopId': 't061-recording-demo', 'goalContext': code3['goalContext'],
               'problemRef': goal['problemRef'], 'deadlineAt': (now() + timedelta(seconds=90)).isoformat()}
    service = PublicRunService(path, args.output / 'work')
    started = time.monotonic()
    try:
        run = service.run(context, code3)
        run_error = None
    except Exception as error:
        run = None
        # No raw infrastructure error/log text is published.
        run_error = type(error).__name__
    store = SQLiteObservationStore(path, public_task_db=service.path)
    organizer = EventOrganizer(store, policy=policy)
    before = business_snapshot(path)
    processed = organizer.process(goal_id, now=now())
    observations = store.list_observations(goal_id)
    again = organizer.process(goal_id, now=now())
    after = business_snapshot(path)
    events = SQLiteEventStore(path).list_goal(goal_id)
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        formal_counts = {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                         if table in tables else 0 for table in ('submissions', 'oj_results')}
    report = {
        'fixture': 'Developer-created two-sum scenario, not a learner session or frontend autosave.',
        'realOperations': ['four saves through SubmissionRepository', 'Docker public run of v3'],
        'simulatedOperations': ['runRequested click: developerFixture, explicit v3; no claimed run ID association'],
        'timing': 'Actual receipt timestamps and a real 2.1 second quiet period; raw facts never rewritten.',
        'elapsedRunSeconds': time.monotonic() - started,
        'run': run, 'runErrorType': run_error, 'clickReceipt': receipt,
        'events': events, 'observations': observations, 'formalRecordCounts': formal_counts,
        'pendingBeforeRestart': pending_before_restart, 'pendingAfterRestart': pending_after_restart,
        'processResult': processed, 'repeatProcessResult': again,
        'failures': store.list_failures(goal_id), 'summaries': store.list_summaries(goal_id),
        'checks': {
            'noFormalJudgingSideEffects': all(count == 0 for count in formal_counts.values()),
            'pendingRecovered': bool(pending_before_restart) and pending_before_restart == pending_after_restart,
            'simulatedBoundaryAccepted': receipt['status'] == 'recorded',
            'repeatDoesNotDuplicate': observations == store.list_observations(goal_id),
            'businessRowsUnchangedByOrganizer': before == after,
            'publicSamplesPassed': bool(run) and run.get('sampleStatus') == 'passed',
            'v2ThroughV4Grouped': any(item['kind'] == 'codeChange'
                and item['evidence']['startVersion'] == 1 and item['evidence']['endVersion'] == 4
                and len(item['sourceEventIds']) == 3 for item in observations),
            'oldResultStillVersion3': any(item['kind'] == 'publicRunFinished'
                and item['evidence']['codeVersion'] == 3 for item in observations),
            'noObservationFailures': not store.list_failures(goal_id),
            'publicResultRecorded': any(row['event']['eventName'] == 'publicRunFinished' for row in events),
        }}
    (args.output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report['checks'], ensure_ascii=False))
    return 0 if all(report['checks'].values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
