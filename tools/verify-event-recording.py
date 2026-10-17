"""Isolated T060 event recording demonstration with real Docker public execution."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src'), str(ROOT / 'tools')]
from goal_compilation_examples import seed_example
from apps.api.submission_storage import SubmissionRepository
from services.oj_worker.tool_public_runs import PublicRunService
from focusmate.adapters.raw_events import SQLiteEventStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    db_path = args.output / 'fixture.sqlite3'
    goal, frozen, _ = seed_example(db_path, 'two-sum')
    repo = SubmissionRepository(db_path)
    source = 'n, target = map(int, input().split())\na = list(map(int, input().split()))\nseen = {}\nfor j, value in enumerate(a):\n    if target - value in seen:\n        print(seen[target - value], j)\n        break\n    seen[value] = j\n'
    code = repo.create_code_snapshot(goal['goalId'], source, 'python3')
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE public_test_cases(test_case_id TEXT,asset_id TEXT,asset_version INTEGER,problem_id TEXT,problem_version INTEGER,example_index INTEGER,stdin_text TEXT,expected_output_text TEXT)')
        for i, example in enumerate(frozen['problemPackage']['examples']):
            db.execute('INSERT INTO public_test_cases VALUES(?,?,?,?,?,?,?,?)',
                (f'example-{i}', 'developer-public-examples', 1, goal['problemRef']['problemId'], 1, i, example['input'], example['output']))
    context = {'loopId': 't060-recording-demo', 'goalContext': code['goalContext'],
        'problemRef': goal['problemRef'], 'deadlineAt': (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()}
    service = PublicRunService(db_path, args.output / 'work')
    before = time.monotonic()
    result = service.run(context, code)
    first = service.event_receipts
    repeated = service.record_events()
    with sqlite3.connect(db_path) as db:
        count = db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0]
        task_count = sqlite3.connect(service.path)
        try:
            tasks = task_count.execute('SELECT COUNT(*) FROM tool_public_tasks').fetchone()[0]
        finally:
            task_count.close()
    report = {'fixture': 'developer-created two-sum materials; not a real learner session',
        'execution': 'real Docker', 'elapsedSeconds': time.monotonic() - before,
        'run': result, 'initialReceipts': first, 'repeatReceipts': repeated,
        'events': SQLiteEventStore(db_path).list_goal(goal['goalId']),
        'submissionCount': count, 'publicTaskCount': tasks,
        'checks': {'terminalEventRecorded': bool(first) and first[0]['status'] == 'recorded',
            'repairIsDuplicate': bool(repeated) and repeated[0]['status'] == 'duplicate',
            'oneTaskNoSubmission': tasks == 1 and count == 0,
            'publicSamplesPassed': result.get('sampleStatus') == 'passed'}}
    (args.output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report['checks'], ensure_ascii=False))


if __name__ == '__main__':
    main()
