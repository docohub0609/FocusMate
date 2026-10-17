"""Organize saved events once or watch for new events; no model or code execution."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from focusmate.adapters.observations import SQLiteObservationStore
from focusmate.contracts.observations import OrganizationPolicy
from focusmate.core.event_organizer import EventOrganizer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', '--database', type=Path, required=True)
    parser.add_argument('--goal-id', '--goal', required=True)
    parser.add_argument('--public-task-db', type=Path)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--poll-ms', type=int, default=250)
    parser.add_argument('--quiet-seconds', type=float, default=2)
    parser.add_argument('--max-seconds', type=float, default=30)
    args = parser.parse_args()
    try:
        if args.poll_ms <= 0:
            raise ValueError('poll interval must be positive')
        policy = OrganizationPolicy(quiet_seconds=args.quiet_seconds, max_seconds=args.max_seconds)
        store = SQLiteObservationStore(args.db, public_task_db=args.public_task_db)
        worker = EventOrganizer(store, policy=policy)
        while True:
            result = worker.process(args.goal_id, now=datetime.now(timezone.utc))
            result.update(observations=store.list_observations(args.goal_id), pending=store.list_pending(args.goal_id),
                          failuresDetail=store.list_failures(args.goal_id), summaries=store.list_summaries(args.goal_id))
            if not args.watch or result['processed'] or result['created']:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if not args.watch:
                return 0
            time.sleep(args.poll_ms / 1000)
    except KeyboardInterrupt:
        # Pending groups remain durable; stopping a watcher is not an editing boundary.
        return 0
    except (ValueError, sqlite3.Error) as error:
        print(json.dumps({'status': 'failed', 'reason': type(error).__name__, 'message': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
