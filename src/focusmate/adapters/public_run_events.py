"""Retry only event delivery from durable terminal tasks, never public execution."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .raw_events import SQLiteEventStore, event_from_record


def capture_contract_version(business_path, goal_context):
    """Capture subgoal identity at execution start, never choose a later contract."""
    if goal_context.get('scope') != 'subgoal':
        return None
    try:
        path = Path(business_path).resolve()
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute("SELECT version,compilation_json FROM goal_contract_versions WHERE goal_id=? AND status='frozen'", (goal_context['goalId'],)).fetchone()
        if row and goal_context['subgoalId'] in {s['subgoalId'] for s in json.loads(row[1])['contract']['subgoals']}:
            return row[0]
    except (sqlite3.Error, ValueError, KeyError):
        pass
    return None


def backfill_public_run_events(business_path, task_path, *, run_id=None):
    business_path, task_path = Path(business_path).resolve(), Path(task_path).resolve()
    if not business_path.is_file() or not task_path.is_file():
        return [{'status': 'rejected', 'reason': 'EVENT_SOURCE_UNAVAILABLE'}]
    try:
        with closing(sqlite3.connect(task_path.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT t.*,m.finished_at,c.contract_version FROM tool_public_tasks t LEFT JOIN tool_public_event_metadata m ON m.run_id=t.run_id LEFT JOIN tool_public_event_context c ON c.run_id=t.run_id "
                "WHERE t.status IN ('succeeded','failed','cancelled','interrupted')" + (' AND t.run_id=?' if run_id else ''),
                (run_id,) if run_id else ()).fetchall()
        receipts = []
        for row in rows:
            if not row['finished_at']:
                receipts.append({'status': 'rejected', 'reason': 'EVENT_SOURCE_TIME_UNAVAILABLE', 'runId': row['run_id']})
                continue
            identity = json.loads(row['context_json'])
            result = json.loads(row['result_json']) if row['result_json'] else {}
            if any(result.get(k, v) != v for k, v in identity.items()) or result.get('status', row['status']) != row['status']:
                receipts.append({'status': 'rejected', 'reason': 'EVENT_REFERENCE_INVALID', 'runId': row['run_id']})
                continue
            record = dict(identity, runId=row['run_id'], status=row['status'], createdAt=row['finished_at'])
            if result.get('sampleStatus'):
                record['sampleStatus'] = result['sampleStatus']
            event = event_from_record('publicRunFinished', record)
            if event['goalContext']['scope'] == 'subgoal':
                if not row['contract_version']:
                    receipts.append({'status': 'rejected', 'reason': 'EVENT_CONTEXT_INVALID', 'runId': row['run_id']})
                    continue
                event['contractVersion'] = row['contract_version']
            # The expected event is derived from a read-only task row above, never
            # from caller-supplied verdict/output. No hidden or actual output copied.
            resolver = lambda incoming, expected=event: incoming == expected
            store = SQLiteEventStore(business_path, reference_resolver=resolver)
            receipts.append(dict(store.receive(event, trusted_source='toolExecutor'), runId=row['run_id']))
        return receipts
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        return [{'status': 'rejected', 'reason': 'EVENT_SOURCE_UNAVAILABLE'}]
